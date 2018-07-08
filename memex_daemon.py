#!/usr/bin/env python

import copy
import json
import math
import os.path
import random
import re

from datetime import datetime
from datetime import timedelta

from rtmapi import Rtm as rtm

RTM_KEYS_FILEPATH = '~/.walros/memex/keys.json'


class Task(object):
    def __init__(self, task_id=None, task_name=None):
        self.id = task_id
        self.name = task_name
        self.due = None
        self.added = None
        self.priority = 4
        self.estimate = None
        self.postponed = None
        self.completed = None
        self.url = None
        self.tags = []
        self.notes = []

        # RTM specific fields
        self.list_id = None
        self.taskseries_id = None
        self.task_id = None

    @classmethod
    def generate_task_id(class_obj, prefix, num_digits):
        task_num = random.randint(0, math.pow(10, num_digits) - 1)
        task_num_str = str(task_num).zfill(num_digits)
        return prefix + task_num_str

    @classmethod
    def generate_task_regex(class_obj, prefix):
        '''All Task IDs match the pattern {prefix}[0-9]+'''
        return re.compile(r'^%s([0-9]+)$' % prefix)


class Milk(object):
    def __init__(self, api_key, secret, token, perms):
        self.__rtmapi = rtm(api_key, secret, perms, token)

    def tasks(self, selector):
        tasks = []
        result = self.__rtmapi.rtm.tasks.getList(filter=selector)
        for tasklist in result.tasks:
            for taskseries in tasklist:
                # TODO: there can be multiple tasks per task series? wat
                task = Task()
                Milk.__set_fields_from_rtm(task, tasklist.id, taskseries)
                tasks.append(task)
        return tasks

    def create_task(self, task):
        entry = task.name
        if task.due:
            entry += ' ^%s' % task.due.isoformat()
        if task.priority:
            entry += ' !%d' % task.priority
        if task.estimate:
            entry += ' =%s' % task.estimate
        if task.tags:
            if task.id and task.id not in task.tags:
                task.tags.append(task.id)
            for tag in task.tags:
                entry += ' #%s' % tag

        timeline = self.__create_timeline()
        ret = self.__rtmapi.rtm.tasks.add(timeline=timeline, parse='1',
                                          name=entry)
        task.list_id = ret.list.id
        task.taskseries_id = ret.list.taskseries.id
        task.task_id = ret.list.taskseries.task.id

        if task.completed:
            self.__rtmapi.rtm.tasks.complete(
                timeline=timeline, list_id=task.list_id,
                taskseries_id=task.taskseries_id, task_id=task.task_id)

        if task.url:
            self.__rtmapi.rtm.tasks.setURL(
                timeline=timeline, list_id=task.list_id,
                taskseries_id=task.taskseries_id, task_id=task.task_id,
                url=task.url)

        for note in task.notes:
            self.__rtmapi.rtm.tasks.notes.add(
                timeline=timeline, list_id=task.list_id,
                taskseries_id=task.taskseries_id, task_id=task.task_id,
                note_title=note[0], note_text=note[1])

    def set_tags(self, task, tags):
        if not task.list_id or not task.taskseries_id or not task.task_id:
            raise Exception('Milk: task rtm fields uninitialized')

        if task.id and task.id not in tags:
            tags.append(task.id)

        timeline = self.__create_timeline()
        self.__rtmapi.rtm.tasks.setTags(
            timeline=timeline, list_id=task.list_id,
            taskseries_id=task.taskseries_id, task_id=task.task_id,
            tags=','.join(tags))

    def __create_timeline(self):
        return self.__rtmapi.rtm.timelines.create().timeline.value

    @classmethod
    def __parse_rtm_date(class_obj, datestr):
        if not datestr:
            return None

        # TODO: factor out format constant
        return datetime.strptime(datestr, '%Y-%m-%dT%H:%M:%SZ')

    @classmethod
    def __set_fields_from_rtm(class_obj, task, list_id, rtm_taskseries):
        # TODO: these should be set to None if not set in rtm

        task.name = rtm_taskseries.name
        task.due = Milk.__parse_rtm_date(rtm_taskseries.task.due)
        task.added = Milk.__parse_rtm_date(rtm_taskseries.task.added)
        task.priority = rtm_taskseries.task.priority
        if task.priority == 'N':
            task.priority = 4
        else:
            task.priority = int(task.priority)

        task.estimate = rtm_taskseries.task.estimate
        task.postponed = rtm_taskseries.task.postponed
        task.completed = Milk.__parse_rtm_date(rtm_taskseries.task.completed)
        task.url = rtm_taskseries.url
        task.tags = []
        for tag in rtm_taskseries.tags:
            task.tags.append(tag.value)

        task.notes = []
        for note in rtm_taskseries.notes:
            task.notes.append((note.title, note.value))

        # RTM specific fields
        task.list_id = list_id
        task.taskseries_id = rtm_taskseries.id
        task.task_id = rtm_taskseries.task.id

def init_milk():
    # read keys; TODO: key path should be in config
    keys = None
    with open(os.path.expanduser(RTM_KEYS_FILEPATH)) as f:
        keys = json.loads(f.read())

    # initialize api
    api_key = keys['rtm_api_key']
    secret = keys['rtm_secret']
    token = keys['rtm_token']
    milk = Milk(api_key, secret, token, 'delete')
    return milk

def id_from_tags(tags, id_prefix):
    task_id = None
    id_regex = Task.generate_task_regex(id_prefix)
    for tag in tags:
        if id_regex.match(tag):
            task_id = tag

    return task_id

def memex(milk):
    # TODO: factor out 'memex' tag constant
    tasks = milk.tasks('tag:memex and status:completed')
    interval_regex = Task.generate_task_regex('s')

    for task in tasks:

        print task.name

        # TODO: factor out prefix constant
        task.id = id_from_tags(task.tags, 'z')
        if not task.id:
            task.id = Task.generate_task_id('z', 6)

        # move current task to memex-archive
        extraneous_tags = []
        for t in task.tags:
            if t == task.id or t == 'memex' or interval_regex.match(t):
                continue

            extraneous_tags.append(t)

        archive_tags = extraneous_tags + ['memex-archive']
        milk.set_tags(copy.deepcopy(task), archive_tags)

        # extract interval size from tags
        interval = None
        for tag in task.tags:
            match = interval_regex.match(tag)
            if match:
                interval = int(match.groups()[0])

        if interval == 0:
            # task is no longer of interest
            continue

        if not interval:
            interval = 4

        # create next task in review series
        task.due = task.completed + timedelta(interval)
        task.completed = None
        task.tags = ['memex', 's%d' % (interval * 2)]
        task.tags += extraneous_tags
        task.priority = 3

        milk.create_task(task)


if __name__ == '__main__':
    milk = init_milk()
    memex(milk)
