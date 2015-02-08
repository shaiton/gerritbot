#! /usr/bin/env python

#    Copyright 2011 OpenStack LLC
#    Copyright 2012 Hewlett-Packard Development Company, L.P.
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

# The configuration file should look like:
"""
[ircbot]
nick=NICKNAME
pass=PASSWORD
server=irc.freenode.net
port=6667
force_ssl=false
server_password=SERVERPASS
channel_config=/path/to/yaml/config
pid=/path/to/pid_file

[gerrit]
user=gerrit2
key=/path/to/id_rsa
host=review.example.com
port=29418
"""

# The yaml channel config should look like:
"""
openstack-dev:
    events:
      - patchset-created
      - change-merged
    projects:
      - openstack/nova
      - openstack/swift
    branches:
      - master
"""

import ConfigParser
import daemon
import irc.bot
import logging.config
import os
import re
import six
import ssl
import sys
import threading
import time
import yaml

try:
    import daemon.pidlockfile
    pid_file_module = daemon.pidlockfile
except Exception:
    # as of python-daemon 1.6 it doesn't bundle pidlockfile anymore
    # instead it depends on lockfile-0.9.1
    import daemon.pidfile
    pid_file_module = daemon.pidfile


# https://bitbucket.org/jaraco/irc/issue/34/
# irc-client-should-not-crash-on-failed
# ^ This is why pep8 is a bad idea.
irc.client.ServerConnection.buffer_class.errors = 'replace'


class GerritBot(irc.bot.SingleServerIRCBot):
    def __init__(self, channels, nickname, password, server, port=6667,
                 force_ssl=False, server_password=None):
        if force_ssl or port == 6697:
            factory = irc.connection.Factory(wrapper=ssl.wrap_socket)
            super(GerritBot, self).__init__([(server, port, server_password)],
                                            nickname, nickname,
                                            connect_factory=factory)
        else:
            super(GerritBot, self).__init__([(server, port, server_password)],
                                            nickname, nickname)
        self.channel_list = channels
        self.nickname = nickname
        self.password = password
        self.log = logging.getLogger('gerritbot')

    def on_nicknameinuse(self, c, e):
        self.log.info('Nick previously in use, recovering.')
        c.nick(c.get_nickname() + "_")
        c.privmsg("nickserv", "identify %s " % self.password)
        c.privmsg("nickserv", "ghost %s %s" % (self.nickname, self.password))
        c.privmsg("nickserv", "release %s %s" % (self.nickname, self.password))
        time.sleep(1)
        c.nick(self.nickname)
        self.log.info('Nick previously in use, recovered.')

    def on_welcome(self, c, e):
        self.log.info('Identifying with IRC server.')
        c.privmsg("nickserv", "identify %s " % self.password)
        self.log.info('Identified with IRC server.')
        for channel in self.channel_list:
            c.join(channel)
            self.log.info('Joined channel %s' % channel)
            time.sleep(0.5)

    def send(self, channel, msg):
        self.log.info('Sending "%s" to %s' % (msg, channel))
        try:
            self.connection.privmsg(channel, msg)
            time.sleep(0.5)
        except Exception:
            self.log.exception('Exception sending message:')
            self.reconnect()


class Gerrit(threading.Thread):
    def __init__(self, ircbot, channel_config, server,
                 username, port=29418, keyfile=None):
        super(Gerrit, self).__init__()
        self.ircbot = ircbot
        self.channel_config = channel_config
        self.log = logging.getLogger('gerritbot')
        self.server = server
        self.username = username
        self.port = port
        self.keyfile = keyfile
        self.connected = False

    def connect(self):
        # Import here because it needs to happen after daemonization
        import gerritlib.gerrit
        try:
            self.gerrit = gerritlib.gerrit.Gerrit(
                self.server, self.username, self.port, self.keyfile)
            self.gerrit.startWatching()
            self.log.info('Start watching Gerrit event stream.')
            self.connected = True
        except Exception:
            self.log.exception('Exception while connecting to gerrit')
            self.connected = False
            # Delay before attempting again.
            time.sleep(1)

    def patchset_created(self, channel, data):
        msg = '%s proposed %s: %s  %s' % (
            data['patchSet']['uploader']['username'],
            data['change']['project'],
            data['change']['subject'],
            data['change']['url'])
        self.log.info('Compiled Message %s: %s' % (channel, msg))
        self.ircbot.send(channel, msg)

    def ref_updated(self, channel, data):
        refName = data['refUpdate']['refName']
        m = re.match(r'(refs/tags)/(.*)', refName)

        if m:
            tag = m.group(2)
            msg = '%s tagged project %s with %s' % (
                data['submitter']['username'],
                data['refUpdate']['project'],
                tag
            )
            self.log.info('Compiled Message %s: %s' % (channel, msg))
            self.ircbot.send(channel, msg)

    def comment_added(self, channel, data):
        msg = 'A comment has been added to a proposed change to %s: %s  %s' % (
            data['change']['project'],
            data['change']['subject'],
            data['change']['url'])
        self.log.info('Compiled Message %s: %s' % (channel, msg))
        self.ircbot.send(channel, msg)

        for approval in data.get('approvals', []):
            if (approval['type'] == 'VRIF' and approval['value'] == '-2'
                and channel in self.channel_config.events.get(
                    'x-vrif-minus-2', set())):
                msg = 'Verification of a change to %s failed: %s  %s' % (
                    data['change']['project'],
                    data['change']['subject'],
                    data['change']['url'])
                self.log.info('Compiled Message %s: %s' % (channel, msg))
                self.ircbot.send(channel, msg)

            if (approval['type'] == 'VRIF' and approval['value'] == '2'
                and channel in self.channel_config.events.get(
                    'x-vrif-plus-2', set())):
                msg = 'Verification of a change to %s succeeded: %s  %s' % (
                    data['change']['project'],
                    data['change']['subject'],
                    data['change']['url'])
                self.log.info('Compiled Message %s: %s' % (channel, msg))
                self.ircbot.send(channel, msg)

            if (approval['type'] == 'CRVW' and approval['value'] == '-2'
                and channel in self.channel_config.events.get(
                    'x-crvw-minus-2', set())):
                msg = 'A change to %s has been rejected: %s  %s' % (
                    data['change']['project'],
                    data['change']['subject'],
                    data['change']['url'])
                self.log.info('Compiled Message %s: %s' % (channel, msg))
                self.ircbot.send(channel, msg)

            if (approval['type'] == 'CRVW' and approval['value'] == '2'
                and channel in self.channel_config.events.get(
                    'x-crvw-plus-2', set())):
                msg = 'A change to %s has been approved: %s  %s' % (
                    data['change']['project'],
                    data['change']['subject'],
                    data['change']['url'])
                self.log.info('Compiled Message %s: %s' % (channel, msg))
                self.ircbot.send(channel, msg)

    def change_merged(self, channel, data):
        msg = 'Merged %s: %s  %s' % (
            data['change']['project'],
            data['change']['subject'],
            data['change']['url'])
        self.log.info('Compiled Message %s: %s' % (channel, msg))
        self.ircbot.send(channel, msg)

    def _read(self, data):
        try:
            if data['type'] == 'ref-updated':
                channel_set = self.channel_config.events.get('ref-updated')
            else:
                channel_set = (self.channel_config.events.get(
                        data['type'], set()) &
                    self.channel_config.branches.get(
                        data['change']['branch'], set()))
        except KeyError:
            # The data we care about was not present, no channels want
            # this event.
            channel_set = set()
        self.log.info('Potential channels to receive event notification: %s' %
                      channel_set)
        for channel in channel_set:
            if data['type'] == 'comment-added':
                self.comment_added(channel, data)
            elif data['type'] == 'patchset-created':
                self.patchset_created(channel, data)
            elif data['type'] == 'change-merged':
                self.change_merged(channel, data)
            elif data['type'] == 'ref-updated':
                self.ref_updated(channel, data)

    def run(self):
        while True:
            while not self.connected:
                self.connect()
            try:
                event = self.gerrit.getEvent()
                self.log.info('Received event: %s' % event)
                self._read(event)
            except Exception:
                self.log.exception('Exception encountered in event loop')
                if not self.gerrit.watcher_thread.is_alive():
                    # Start new gerrit connection. Don't need to restart IRC
                    # bot, it will reconnect on its own.
                    self.connected = False


class ChannelConfig(object):
    def __init__(self, data):
        self.data = data
        keys = data.keys()
        for key in keys:
            if key[0] != '#':
                data['#' + key] = data.pop(key)
        self.channels = data.keys()
        self.projects = {}
        self.events = {}
        self.branches = {}
        for channel, val in six.iteritems(self.data):
            for event in val['events']:
                event_set = self.events.get(event, set())
                event_set.add(channel)
                self.events[event] = event_set
            for project in val['projects']:
                project_set = self.projects.get(project, set())
                project_set.add(channel)
                self.projects[project] = project_set
            for branch in val['branches']:
                branch_set = self.branches.get(branch, set())
                branch_set.add(channel)
                self.branches[branch] = branch_set


def _main(config):
    setup_logging(config)

    fp = config.get('ircbot', 'channel_config')
    if fp:
        fp = os.path.expanduser(fp)
        if not os.path.exists(fp):
            raise Exception("Unable to read layout config file at %s" % fp)
    else:
        raise Exception("Channel Config must be specified in config file.")

    channel_config = ChannelConfig(yaml.load(open(fp)))

    bot = GerritBot(channel_config.channels,
                    config.get('ircbot', 'nick'),
                    config.get('ircbot', 'pass'),
                    config.get('ircbot', 'server'),
                    config.getint('ircbot', 'port'),
                    config.getboolean('ircbot', 'force_ssl'),
                    config.get('ircbot', 'server_password'))
    g = Gerrit(bot,
               channel_config,
               config.get('gerrit', 'host'),
               config.get('gerrit', 'user'),
               config.getint('gerrit', 'port'),
               config.get('gerrit', 'key'))
    g.start()
    bot.start()


def main():
    if len(sys.argv) != 2:
        print "Usage: %s CONFIGFILE" % sys.argv[0]
        sys.exit(1)

    config = ConfigParser.ConfigParser({'force_ssl': 'false',
                                        'server_password': None})
    config.read(sys.argv[1])

    pid_path = ""
    if config.has_option('ircbot', 'pid'):
        pid_path = config.get('ircbot', 'pid')
    else:
        pid_path = "/var/run/gerritbot/gerritbot.pid"

    pid = pid_file_module.TimeoutPIDLockFile(pid_path, 10)
    with daemon.DaemonContext(pidfile=pid):
        _main(config)


def setup_logging(config):
    if config.has_option('ircbot', 'log_config'):
        log_config = config.get('ircbot', 'log_config')
        fp = os.path.expanduser(log_config)
        if not os.path.exists(fp):
            raise Exception("Unable to read logging config file at %s" % fp)
        logging.config.fileConfig(fp)
    else:
        logging.basicConfig(level=logging.DEBUG)


if __name__ == "__main__":
    main()
