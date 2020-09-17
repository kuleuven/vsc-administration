# -*- coding: latin-1 -*-
#
# Copyright 2013-2020 Ghent University
#
# This file is part of vsc-administration,
# originally created by the HPC team of Ghent University (http://ugent.be/hpc/en),
# with support of Ghent University (http://ugent.be/hpc),
# the Flemish Supercomputer Centre (VSC) (https://www.vscentrum.be),
# the Flemish Research Foundation (FWO) (http://www.fwo.be/en)
# and the Department of Economy, Science and Innovation (EWI) (http://www.ewi-vlaanderen.be/en).
#
# https://github.com/hpcugent/vsc-administration
#
# All rights reserved.
#
"""
This module contains tools to sync accountpage users to the vsc ldap
"""
from __future__ import absolute_import
import logging

from vsc.utils.py2vs3 import HTTPError

import pytz as timezone
from datetime import datetime

from ldap import LDAPError

from vsc.accountpage.wrappers import mkVscAccount, mkUserGroup, mkGroup, mkVo
from vsc.config.base import VSC, INSTITUTE_VOS_GENT
from vsc.ldap.entities import VscLdapUser, VscLdapGroup
from vsc.ldap.filters import CnFilter
from vsc.utils.py2vs3 import ensure_ascii_string

ACCOUNT_WITHOUT_PUBLIC_KEYS_MAGIC_STRING = "THIS ACCOUNT HAS NO VALID PUBLIC KEYS"

DONE = 'done'
NEW = 'new'
UPDATED = 'updated'
ERROR = 'error'

VSC_CONFIG = VSC()

class LdapSyncer(object):
    """
    This class implements a system for syncing changes from the accountpage api
    to the vsc ldap
    """
    def __init__(self, client):
        """
        Create an ldap syncer, requires a RestClient client to get the information from
        (typically AccountpageClient)
        """
        self.client = client
        self.now = datetime.utcnow().replace(tzinfo=timezone.utc)

    def add_or_update(self, VscLdapKlass, cn, ldap_attributes, dry_run):
        """
        Perform the update in LDAP for the given vsc.ldap.entitities class, cn and the ldap attributes.

        @return: NEW, UPDATED or ERROR, depending on the operation and its result.
        """
        ldap_entries = VscLdapKlass.lookup(CnFilter(cn))
        if not ldap_entries:
            # add the entry
            logging.debug("add new entry %s %s with the attributes %s", VscLdapKlass.__name__, cn, ldap_attributes)

            if not dry_run:
                try:
                    entry = VscLdapKlass(cn)
                    entry.add(ldap_attributes)
                    logging.info("Added a new user %s to LDAP" % (cn,))
                except LDAPError:
                    logging.warning("Could not add %s %s to LDAP" % (VscLdapKlass.__name__, cn,))
                    return ERROR
            return NEW
        else:
            ldap_entries[0].status
            logging.debug("update existing entry %s %s with the attributes %s -- old entry: %s",
                          VscLdapKlass.__name__, cn, ldap_attributes, ldap_entries[0].ldap_info)

            if not dry_run:
                try:
                    ldap_entries[0].modify_ldap(ldap_attributes)
                    logging.info("Modified %s %s in LDAP" % (VscLdapKlass.__name__, cn,))
                except LDAPError:
                    logging.warning("Could not add %s %s to LDAP" % (VscLdapKlass.__name__, cn,))
                    return ERROR
            return UPDATED

    def sync_altered_accounts(self, last, dry_run=True):
        """
        Add new users to the LDAP and update altered users. This does not include usergroups.

        this does include pubkeys
        @type last: datetime
        @return: tuple (new, updated, error) that indicates what accounts were new, changed or could not be altered.
        """
        sync_accounts = [mkVscAccount(a) for a in self.client.account.modified[last].get()[1]]
        accounts = {
            NEW: set(),
            UPDATED: set(),
            ERROR: set(),
        }

        logging.info("Found %d modified accounts in the range %s until %s" % (len(sync_accounts),
                     datetime.fromtimestamp(last).strftime("%Y%m%d%H%M%SZ"),
                     self.now.strftime("%Y%m%d%H%M%SZ")))
        logging.debug("Modified accounts: %s", [a.vsc_id for a in sync_accounts])

        for account in sync_accounts:
            try:
                usergroup = mkUserGroup(self.client.account[account.vsc_id].usergroup.get()[1])
            except HTTPError:
                logging.error("No corresponding UserGroup for user %s" % (account.vsc_id,))
                continue
            gecos = ensure_ascii_string(account.person.gecos)

            logging.debug('fetching public key')
            public_keys = [ensure_ascii_string(x.pubkey) for x in self.client.get_public_keys(account.vsc_id)]

            if not public_keys:
                public_keys = [ACCOUNT_WITHOUT_PUBLIC_KEYS_MAGIC_STRING]

            LDAP_STATE_MAPPER = {
                'forceinactive': 'inactive'
            }

            ldap_attributes = {
                'cn': str(account.vsc_id),
                'uidNumber': ["%s" % (account.vsc_id_number,)],
                'gecos': [gecos],
                'mail': [str(account.email)],
                'institute': [str(account.person.institute['name'])],
                'instituteLogin': [str(account.person.institute_login)],
                'uid': [str(account.vsc_id)],
                'homeDirectory': [str(account.home_directory)],
                'dataDirectory': [str(account.data_directory)],
                'scratchDirectory': [str(account.scratch_directory)],
                'pubkey': public_keys,
                'gidNumber': [str(usergroup.vsc_id_number)],
                'loginShell': [str(account.login_shell)],
                'researchField': [str(account.research_field[0])],
                'status': [LDAP_STATE_MAPPER.get(str(account.status), str(account.status))],
                'homeQuota': ["1"],
                'dataQuota': ["1"],
                'scratchQuota': ["1"],
            }
            logging.debug('fetching quota')
            quotas = self.client.account[account.vsc_id].quota.get()[1]
            for quota in quotas:
                for stype in ['home', 'data', 'scratch']:
                    # only gent sets filesets for vo's, so not gvo is user. (other institutes is empty or "None"
                    if quota['storage']['storage_type'] == stype and not quota['fileset'].startswith('gvo'):
                        ldap_attributes['%sQuota' % stype] = ["%d" % quota["hard"]]

            result = self.add_or_update(VscLdapUser, account.vsc_id, ldap_attributes, dry_run)
            accounts[result].add(account.vsc_id)

        return accounts

    def sync_altered_groups(self, last, dry_run=True):
        """
        Synchronise altered groups back to LDAP.
        This also includes usergroups
        """
        changed_groups = [mkGroup(a) for a in self.client.allgroups.modified[last].get()[1]]

        logging.info("Found %d modified groups in the range %s until %s" % (len(changed_groups),
                     datetime.fromtimestamp(last).strftime("%Y%m%d%H%M%SZ"),
                     self.now.strftime("%Y%m%d%H%M%SZ")))
        logging.debug("Modified groups: %s", [g.vsc_id for g in changed_groups])
        groups = {
            NEW: set(),
            UPDATED: set(),
            ERROR: set(),
        }

        for group in changed_groups:
            vo = False
            try:
                result = self.client.vo[group.vsc_id].get()[1]
                # if result is empty, the group is not an VO.
                if result:
                    vo = mkVo(result)
            except HTTPError as err:
                # if a 404 occured, the group is not an VO, so we skip this. Otherwise something else went wrong.
                if err.code != 404:
                    raise
            group_moderators = [str(m) for m in group.moderators]
            institute_name = str(group.institute['name'])
            if not group_moderators:
                group_moderators = [str(VSC_CONFIG.backup_group_mods[group.institute['name']])]
                logging.info("Using backup moderator %s for group %s", group_moderators, group.vsc_id)
            ldap_attributes = {
                'cn': str(group.vsc_id),
                'institute': [institute_name],
                'gidNumber': ["%d" % (group.vsc_id_number,)],
                'moderator': group_moderators,
                'memberUid': [str(a) for a in group.members],
                'status': [str(group.status)],
            }
            if vo:
                ldap_attributes['fairshare'] = ["%d" % (vo.fairshare,)]
                ldap_attributes['description'] = [str(vo.description)]
                ldap_attributes['dataDirectory'] = [str(vo.data_path)]
                ldap_attributes['scratchDirectory'] = [str(vo.scratch_path)]
                # vsc40024 is moderator for all institute vo's
                if vo.vsc_id in INSTITUTE_VOS_GENT.values():
                    ldap_attributes['moderator'] = ['vsc40024']

            logging.debug("Proposed changes for group %s: %s", group.vsc_id, ldap_attributes)

            result = self.add_or_update(VscLdapGroup, group.vsc_id, ldap_attributes, dry_run)
            groups[result].add(group.vsc_id)

        return groups
