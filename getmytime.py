#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division

import os
import re
import sys
import json
import argparse
import logging
import requests
import time
import itertools
import fileinput

from datetime import date, datetime, timedelta


ID_REGEX = re.compile('(?P<id>\d{8})')

log = logging.getLogger(__name__)
log.addHandler(logging.StreamHandler(sys.stderr))
log.setLevel(logging.DEBUG)


def getenv(key):
    try:
        return os.environ[key]
    except KeyError:
        print('Environmental variable required: ' + key)
        sys.exit(1)


def lowerCaseKeys(d):
    return dict((k.lower(), v) for k, v in d.iteritems())


def unescape(d):
    return dict((k.replace('&amp;', '&'),
                 v.replace('&amp;', '&')) for k, v in d.iteritems())


class GetMyTimeError(Exception):
    pass


class InvalidTimeEntryError(Exception):
    pass


class GetMyTimeApi(object):
    URL = 'https://app.getmytime.com/service.aspx'

    def login(self, username, password):
        time.sleep(1)

        params = {
            'object': 'getmytime.api.usermanager',
            'method': 'login',
        }
        form_data = {
            'username': username,
            'password': password,
        }

        r = requests.post(self.URL, params=params, data=form_data)
        payload = r.json()

        if 'error' in payload:
            raise GetMyTimeError(payload)

        self.cookies = r.cookies
        self.fetch_lookups()
        self.detect_top_level_categories()

    def fetch_lookups(self):
        time.sleep(1)

        params = {
            'object': 'getmytime.api.managemanager',
            'method': 'fetchLookups',
        }
        form_data = {
            'lookups': '[customerjobs],[serviceitems]',
        }

        r = requests.post(self.URL, params=params, data=form_data,
                          cookies=self.cookies)

        payload = r.json()
        self.lookups = payload

        lookup = lambda k, a, b: dict((row[a], row[b])
                                      for row in payload[k]['rows'])

        self.lookupById = {
            'tasks': unescape(lookup('serviceitems',
                                     'intTaskListID',
                                     'strTaskName')),
            'customers': unescape(lookup('customerjobs',
                                         'intClientJobListID',
                                         'strClientJobName')),
        }
        self.lookupByName = {
            'tasks': lowerCaseKeys(unescape(lookup('serviceitems',
                                                   'strTaskName',
                                                   'intTaskListID'))),
            'customers': lowerCaseKeys(unescape(lookup('customerjobs',
                                                       'strClientJobName',
                                                       'intClientJobListID'))),
        }

    def detect_top_level_categories(self):
        tasks = self.lookupById['tasks'].values()
        customers = self.lookupById['customers'].values()
        self.topLevelCategories = {
            'tasks': set(parts[0].lower() for parts in
                         (name.split(':') for name in tasks)
                         if len(parts) > 1),
            'customers': set(parts[0].lower() for parts in
                             (name.split(':') for name in customers)
                             if len(parts) > 1),
        }

    def fetch_entries(self, start_date, end_date):
        curdate = start_date

        while curdate < end_date:
            time.sleep(1)

            params = {
                'object': 'getmytime.api.timeentrymanager',
                'method': 'fetchTimeEntries',
            }
            form_data = {
                'employeeid': self.cookies['userid'],
                'startdate': '{:%m/%d/%Y}'.format(curdate),
            }

            r = requests.post(self.URL, params=params, data=form_data,
                              cookies=self.cookies)

            payload = r.json()

            if 'error' in payload:
                raise GetMyTimeError(payload)

            try:
                rows = payload['rows']
            except KeyError:
                # No records were found.
                break

            by_date = lambda entry: entry['entry_date']
            entries = self.parse_entries(rows)
            entries = sorted(entries, key=by_date)

            for entry in entries:
                if entry['entry_date'] < end_date:
                    yield entry

            curdate += timedelta(days=7)

    def create(self, entries, **flags):
        print('Importing {} entries...'.format(len(entries)))
        for entry in entries:
            record = {}
            record.update(entry)
            record.update(flags)
            self.create_time_entry(**record)
        print('Done')

    def create_time_entry(self, startdate, enddate, customer, activity,
                          comments, tags, minutes, dry_run=False,
                          force=False, allow_misc=False):
        customers = self.lookupByName['customers']
        tasks = self.lookupByName['tasks']

        tags = tags if tags else []

        employeeid = self.cookies['userid']
        customerid = customers[customer.lower()]
        taskid = tasks[activity.lower()]
        billable = 'billable' in tags

        params = {
            'object': 'getmytime.api.timeentrymanager',
            'method': 'createTimeEntry',
        }
        form_data = {
            'employeeid': employeeid,
            'startdate': startdate,
            'startdatetime': startdate,
            'minutes': minutes,
            'customerid': customerid,
            'taskid': taskid,
            'comments': comments,
            'billable': billable,
            'projectid': 139,  # Basic
            'classid': 0,
            'starttimer': 'false',
        }

        log.info('Submitting {} {} {}; Notes: {}'.format(
            startdate, customer, activity, comments))

        # Azavea specific rules.

        if len(comments.strip()) == 0:
            raise InvalidTimeEntryError('Comments field may not be empty')

        if activity.lower() in self.topLevelCategories['tasks']:
            raise InvalidTimeEntryError('Not allowed to use top level '
                                        'category "{}"'.format(activity))

        if customer.lower() in self.topLevelCategories['customers']:
            raise InvalidTimeEntryError('Not allowed to use top level '
                                        'category "{}"'.format(customer))

        if activity.lower() == 'Indirect - Admin:Miscellaneous'.lower() and \
                not allow_misc:
            message = 'Never use "Indirect - Admin:Miscellaenous"!'
                      ' (Use `--allow-miscellaneous` to override this rule)'
            raise InvalidTimeEntryError()

        if (not force and
                ('interview' in comments or 'presentation' in comments) and
                'hiring' not in activity.lower()):
            message = 'Consider using "Indirect - Admin:Personnel/Hiring"'
                      ' (Use `--force` to override this rule)'
            raise InvalidTimeEntryError(message)

        if not dry_run:
            r = requests.post(self.URL, params=params, data=form_data,
                              cookies=self.cookies)

            payload = r.json()

            if 'error' in payload:
                raise GetMyTimeError(payload)

            time.sleep(1)

    def parse_entries(self, rows):
        customers = self.lookupById['customers']
        tasks = self.lookupById['tasks']
        for row in rows:
            minutes = int(row['intMinutes'])
            hrs, mins = self.format_minutes(minutes)
            customerId = row['intClientJobListID']
            taskId = row['intTaskListID']

            entry_date = datetime.strptime(row['dtmTimeWorkedDate'],
                                           '%m/%d/%Y %I:%M:%S %p')

            entry_week = entry_date - timedelta(days=entry_date.weekday())

            yield {
                'id': row['intTimeEntryID'],
                'billable': 'Yes' if row['blnBillable'] == 'True' else 'No ',
                'approved': 'Yes' if row['blnApproved'] == 'True' else 'No ',
                'billable_sym': '$' if row['blnBillable'] == 'True' else ' ',
                'approved_sym': '*' if row['blnApproved'] == 'True' else ' ',
                'customer': customers[customerId],
                'task': tasks[taskId],
                'comments': row['strComments'].replace('\n', ' '),
                'entry_date': entry_date,
                'entry_week': entry_week,
                'minutes': minutes,
                'minutes_str': mins,
                'hours_str': hrs,
            }

    def format_minutes(self, minutes):
        hours = minutes // 60
        minutes -= hours * 60
        return (str(hours) + 'h' if hours > 0 else '',
                str(minutes) + 'm' if minutes > 0 else '')

    def get_ls_tmpl(self, show_comments, oneline):
        if oneline:
            tmpl = '{id} {entry_date:%Y-%m-%d} {approved_sym}{billable_sym} ' \
                   '{hours_str:>3}{minutes_str:>3} {customer} > {task}'
            if show_comments:
                tmpl += '; Notes: {comments}'
        else:
            tmpl = 'ID: {id}\nDate: {entry_date:%Y-%m-%d}\nBillable: {billable}\n' \
                   'Approved: {approved}\nCustomer: {customer}\nTask: {task}\n' \
                   'Duration: {hours_str}{minutes_str}\nNotes: {comments}\n'
        return tmpl

    def ls(self, entries, show_comments=False, oneline=False, custom_tmpl=None):
        if custom_tmpl:
            tmpl = custom_tmpl
        else:
            tmpl = self.get_ls_tmpl(show_comments, oneline)

        try:
            for entry in entries:
                print(tmpl.format(**entry))
        except KeyError as ex:
            log.error('Invalid template: Time entries do not have a "{}" field.'.format(ex.message))
        sys.stdout.flush()

    def ls_total(self, entries, args):
        grand_total = 0

        entries = list(entries)
        customer_maxlen = max(len(entry['customer']) for entry in entries)

        group_by_fields = args.group_by.split(',') if args.group_by \
            else ['entry_date']

        row_fmt = []
        for field in group_by_fields:
            if field == 'entry_date':
                row_fmt.append('{0:%Y-%m-%d}')
            elif field == 'entry_week':
                row_fmt.append('{1:%Y-%m-%d}')
            elif field == 'customer':
                row_fmt.append('{2:<' + str(customer_maxlen) + '}')
        row_fmt = ' '.join(row_fmt) + ' {3:>3}{4:>3}'

        entry_key = lambda entry: tuple(entry[k] for k in group_by_fields)
        entries = sorted(entries, key=entry_key)
        grouped_entries = itertools.groupby(entries, key=entry_key)

        for key, entries in grouped_entries:
            entries = list(entries)

            entry_date = entries[0]['entry_date']
            entry_week = entries[0]['entry_week']
            customer = entries[0]['customer']

            total = sum(entry['minutes'] for entry in entries)
            hrs, mins = self.format_minutes(total)
            grand_total += total

            print(row_fmt.format(entry_date, entry_week, customer, hrs, mins))

        sys.stdout.flush()

        hrs, mins = self.format_minutes(grand_total)
        print('{:}{:>3}'.format(hrs, mins))

    def rm(self, ids, dry_run=False):
        time.sleep(1)

        total = 0

        for id in ids:
            log.debug('Deleting {}'.format(id))

            if dry_run:
                continue

            params = {
                'object': 'getmytime.api.timeentrymanager',
                'method': 'deleteTimeEntry',
            }
            form_data = {
                'timeentryid': id,
            }

            r = requests.post(self.URL, params=params, data=form_data,
                              cookies=self.cookies)

            payload = r.json()

            if 'error' in payload:
                raise GetMyTimeError(payload)

            log.info(r.text)
            total += 1

        print('Deleted {} record(s)'.format(total))


def detect_ids(lines):
    """Return list of ids scraped from each line in lines"""
    for line in lines:
        match = ID_REGEX.search(line)
        if match:
            yield int(match.group('id'))


def get_date_range(args):
    if args.today:
        start_date = date.today()
        end_date = start_date + timedelta(days=1)
        return start_date, end_date

    if args.startdate:
        start_date = datetime.strptime(args.startdate, '%Y-%m-%d')
    else:
        # Subtract 6 days so time entries from today appear by default.
        start_date = datetime.now() - timedelta(days=6)

    if args.enddate:
        end_date = datetime.strptime(args.enddate, '%Y-%m-%d')
    else:
        end_date = start_date + timedelta(days=7)

    return start_date, end_date


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(help='sub-command help')

    parser1 = subparsers.add_parser('ls')
    parser1.add_argument('startdate', nargs='?',
                         help='format: YYYY-MM-DD, inclusive (default: today)')
    parser1.add_argument('enddate', nargs='?',
                         help='format: YYYY-MM-DD, exclusive (default: startdate + 7 days)')
    parser1.add_argument('--today', action='store_true',
                         help='show results for today only (overrides --startdate and --enddate)')
    parser1.add_argument('--comments', action='store_true',
                         help='show comments (only relevant for --oneline)')
    parser1.add_argument('--oneline', action='store_true',
                         help='output single line per time entry')
    parser1.add_argument('--tmpl', type=str,
                         help='custom template per time entry')
    parser1.add_argument('--total', action='store_true',
                         help='show daily and weekly totals')
    parser1.add_argument('--group-by',
                         help='group totals by entry_date, entry_week, or customer')
    parser1.set_defaults(cmd='ls')

    parser2 = subparsers.add_parser('rm')
    parser2.add_argument('ids', type=int, nargs='*',
                         help='(defaults to stdin if empty)')
    parser2.add_argument('--dry-run', action='store_true',
                         help='do nothing destructive (useful for testing)')
    parser2.set_defaults(cmd='rm')

    parser3 = subparsers.add_parser('import')
    parser3.add_argument('file', nargs='?', default='-',
                         help='timesheet records JSON (defaults to stdin)')
    parser3.add_argument('--dry-run', action='store_true',
                         help='do nothing destructive (useful for testing)')
    parser3.add_argument('--allow-miscellaneous', action='store_true',
                         help='allow use of Indirect - Admin:Miscellaenous')
    parser3.add_argument('-f', '--force', action='store_true',
                         help='ignore some validation rules')
    parser3.set_defaults(cmd='import')

    parser4 = subparsers.add_parser('lookups')
    parser4.add_argument('--raw', action='store_true',
                         help='output raw values from server')
    parser4.set_defaults(cmd='lookups')

    args = parser.parse_args()

    username = getenv('GETMYTIME_USERNAME')
    password = getenv('GETMYTIME_PASSWORD')

    try:
        api = GetMyTimeApi()
        api.login(username, password)

        if args.cmd == 'ls':
            start_date, end_date = get_date_range(args)
            entries = api.fetch_entries(start_date, end_date)

            if args.total:
                api.ls_total(entries, args)
            else:
                api.ls(entries,
                       show_comments=args.comments,
                       oneline=args.oneline,
                       custom_tmpl=args.tmpl)

        elif args.cmd == 'rm':
            ids = args.ids if args.ids else detect_ids(fileinput.input('-'))
            api.rm(ids, dry_run=args.dry_run)

        elif args.cmd == 'import':
            lines = fileinput.input(args.file)
            contents = ''.join(lines)
            entries = json.loads(contents)
            api.create(entries, dry_run=args.dry_run,
                       force=args.force, allow_misc=args.allow_misc)

        elif args.cmd == 'lookups':
            if args.raw:
                print(json.dumps(api.lookups))
            else:
                print(json.dumps({
                    'lookupByName': api.lookupByName,
                    'lookupById': api.lookupById,
                }))

    except (InvalidTimeEntryError, GetMyTimeError) as ex:
        data = ex.message
        if isinstance(data, basestring):
            log.error('Error: {}'.format(data))
        elif 'message' in data:
            log.error('{}'.format(data['message']))
        elif 'error' in data:
            code = data['error']['code']
            message = data['error']['message']
            log.error('{} {}'.format(code, message))
        else:
            log.exception(ex)
        sys.exit(1)


if __name__ == '__main__':
    main()
