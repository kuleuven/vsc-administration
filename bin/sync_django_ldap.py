#!/usr/bin/env python
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
Get existing Django accountpage users and sync them to the VSC LDAP
"""
import grp
import logging
import os
import pwd
import sys

from vsc.config.base import VSC_CONF_DEFAULT_FILENAME

from vsc.accountpage.client import AccountpageClient

from vsc.administration.ldapsync import LdapSyncer, ERROR

from vsc.ldap.configuration import VscConfiguration
from vsc.ldap.utils import LdapQuery
from vsc.config.base import GENT
from vsc.utils import fancylogger
from vsc.utils.nagios import NAGIOS_EXIT_CRITICAL
from vsc.utils.script_tools import ExtendedSimpleOption
from vsc.utils.timestamp import convert_timestamp, write_timestamp, retrieve_timestamp_with_default

NAGIOS_HEADER = "sync_django_to_ldap"
NAGIOS_CHECK_INTERVAL_THRESHOLD = 15 * 60  # 15 minutes
SYNC_TIMESTAMP_FILENAME = "/var/cache/%s.timestamp" % (NAGIOS_HEADER)
NONROOT_DEFAULT_USER = 'apache'

logger = fancylogger.getLogger()
fancylogger.setLogLevelInfo()
fancylogger.logToScreen(True)


def main():

    options = {
        'nagios-check-interval-threshold': NAGIOS_CHECK_INTERVAL_THRESHOLD,
        'start-timestamp': ("The timestamp form which to start, otherwise use the cached value", None, "store", None),
        'access_token': ('OAuth2 token identifying the user with the accountpage', None, 'store', None),
        'account_page_url': ('URL of the account page', None, 'store', None),
        'host_institute': ('Name of the institute where this script is being run', str, 'store', GENT),
        'start_timestamp': ('Timestamp to start the sync from', str, 'store', None),
        'user': ('System user to run the sync', str, 'store', NONROOT_DEFAULT_USER),
        }
    # get access_token from conf file
    ExtendedSimpleOption.CONFIGFILES_INIT = ['/etc/account_page.conf']
    opts = ExtendedSimpleOption(options)
    stats = {}

    # Creating this here because this is a singleton class
    _ = LdapQuery(VscConfiguration(VSC_CONF_DEFAULT_FILENAME))

    (last_timestamp, start_time) = retrieve_timestamp_with_default(
        SYNC_TIMESTAMP_FILENAME,
        start_timestamp=opts.options.start_timestamp)
    logging.info("Using timestamp %s", last_timestamp)
    logging.info("Using startime %s", start_time)

    try:
        parent_pid = os.fork()
        logging.info("Forked.")
    except OSError:
        logging.exception("Could not fork")
        parent_pid = 1
    except Exception:
        logging.exception("Oops")
        parent_pid = 1

    if parent_pid == 0:
        try:
            global logger
            logger = fancylogger.getLogger(NAGIOS_HEADER)

            # drop privileges in the child
            try:
                child_user = opts.options.user
                child_uid = pwd.getpwnam(child_user).pw_uid
                child_gid = grp.getgrnam(child_user).gr_gid
                os.setgroups([])
                os.setgid(child_gid)
                os.setuid(child_uid)
            except (KeyError, OSError):
                logger.raiseException("Could not drop privileges to user '%s':" % child_user)
            else:
                logging.info("Now running as user %s (uid: %s)", child_user, os.geteuid())

            client = AccountpageClient(token=opts.options.access_token, url=opts.options.account_page_url + '/api')
            syncer = LdapSyncer(client, opts.options.host_institute)
            last = last_timestamp
            altered_accounts = syncer.sync_altered_accounts(last, opts.options.dry_run)

            logging.debug("Altered accounts: %s", altered_accounts)

            altered_groups = syncer.sync_altered_groups(last, opts.options.dry_run)

            logging.debug("Altered groups: %s" % altered_groups)

            if not altered_accounts[ERROR] \
                    and not altered_groups[ERROR]:
                logging.info("Child process exiting correctly")
                sys.exit(0)
            else:
                logging.info("Child process exiting with status -1")
                logging.warning("Error occured in %s" % (
                    ["%s: %s\n" % (k, v) for (k, v) in [
                        ("altered accounts", altered_accounts[ERROR]),
                        ("altered groups", altered_groups[ERROR]),
                    ]]
                ))
                sys.exit(-1)
        except Exception:
            logging.exception("Child caught an exception")
            sys.exit(-1)

    else:
        # parent
        (_, result) = os.waitpid(parent_pid, 0)
        logging.info("Child exited with exit code %d" % (result,))

        if not result and not opts.options.dry_run:
            (_, ldap_timestamp) = convert_timestamp(start_time)
            write_timestamp(SYNC_TIMESTAMP_FILENAME, ldap_timestamp)
            opts.epilogue("Synchronised LDAP users to the Django DB", stats)
        else:
            sys.exit(NAGIOS_EXIT_CRITICAL)


if __name__ == '__main__':
    main()
