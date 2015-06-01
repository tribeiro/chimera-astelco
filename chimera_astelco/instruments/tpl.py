#! /usr/bin/python
# -*- coding: iso-8859-1 -*-

# chimera - observatory automation system
# Copyright (C) 2006-2007  P. Henrique Silva <henrique@astro.ufsc.br>

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.

import time
import os

from chimera.core.chimeraobject import ChimeraObject
from chimera.core.lock import lock
from chimera.core.constants import SYSTEM_CONFIG_DIRECTORY
from chimera.core.exceptions import ChimeraException
from chimera.util.enum import Enum

import telnetlib

__all__ = ["TPLBase"]


class TPLException(ChimeraException):
    pass


TPLStatus = Enum("CONNECTED", "CLOSED")

CMDStatus = Enum("DONE","ABORTED","WAITING","TIMEOUT")

SEND = Enum("OK","ERROR")

class Command():
    id = 0
    cmd = None
    object = None
    received = []
    events = []
    cmdtype = None
    status = None
    allstatus = []
    ok = False
    complete = False
    data = False

    def __str__(self):
        return str(self.id) + ' ' + self.cmd + ' ' + self.object + '\r\n'

class TPL(ChimeraObject):

    __config__ = {"device": '/dev/ttyS0',
                  "tpl_host": 'localhost',
                  "tpl_port": 65432,
                  "user": 'admin',
                  "password": 'admin',
                  "freq": 90,
                  "timeout": 60,
                  "waittime": 0.5}

    def __init__(self):

        ChimeraObject.__init__(self)

        # debug log
        self._debugLog = None
        try:
            self._debugLog = open(
                os.path.join(SYSTEM_CONFIG_DIRECTORY, "tpl-debug.log"), "w")
        except IOError, e:
            self.log.warning("Could not create tpl debug file (%s)" % str(e))

        # Command counter
        self.next_command_id = 1
        # Store received objects
        self.commands_sent = {}


    def __start__(self):

        self.setHz(1.0)

        self.log.debug('tpl START')
        self.open()

        return True

    def __stop__(self):
        self.log.debug('tpl STOP')
        self.close()

    @lock
    def control(self):

        self.log.debug('[control] entering...')

        recv = self.expect()

        nrec = 0

        while recv[1]:
            nrec+=1
            self.log.debug(recv[2])
            recv = self.expect()

        self.log.debug('[control] Received %i commands'%nrec)

        return True

    def expect(self):

        return self.sock.expect(['(?P<CMDID>\d+) COMMAND (?P<STATUS>\S+)\n',
                                 '(?P<CMDID>\d+) DATA INLINE (?P<OBJECT>\S+)=(?P<OBJ>\S+)\n',
                                 '(?P<CMDID>\d+) EVENT ERROR (?P<OBJECT>\S+):(?P<ENCM>(.*?)\s*)\n'],
                                timeout=self['timeout'])

    @lock
    def open(self):  # converted to Astelco
        self.log.info('Connecting to TSI server @ %s:%i' % (self["tpl_host"],
                                                            int(self["tpl_port"])))

        self.connect()

    @lock
    def close(self):  # converted to Astelco

        self.disconnect()


    def connect(self):
        '''
            Connect to tpl server
        '''

        # Open the socket
        self.sock = telnetlib.Telnet(self['tpl_host'], self['tpl_port'], self['timeout'])

        # Read in welcome message up to the end
        s = self.sock.expect(['TPL2\s+(?P<TPL2>\S+)\s+CONN\s+(?P<CONN>\d+)\s+AUTH\s+(?P<AUTH>\S+(,\S+)*)\s+'
                        'ENC MESSAGE (?P<ENCM>(.*?)\s*\\n)'],
                             timeout=self['timeout'])
        if not s:
            self.sock.close()
            raise TPLException(
                'self.sock.connect((' + self.host + str(self.port) + ')', 'Got None as answer.')

        # parse information
        self.protocol_version, self.conn, self.auth_methods, self.encmsg = s[1].group(
            'TPL2'), s[1].group('CONN'), s[1].group('AUTH'), s[1].group('ENCM')

        # Sends credentials
        self.send('AUTH PLAIN "' + self["user"] + '" "' + self["password"] + '"\r\n')
        s = self.sock.expect(['AUTH\s+(?P<AUTH>\S+)\s+(?P<read_level>\d)\s+(?P<write_level>\d)\n'],
                             timeout=self['timeout'])

        if (not s[1]) or (s[1].group('AUTH') != 'OK'):
            self.sock.close()
            raise TPLException('Not authorized.')

        self.read_level, self.write_level = int(
            s[1].group('read_level')), int(s[1].group('write_level'))

    def disconnect(self):
        '''
            Disconnect from tpl server
        '''
        self.log.info( "Disconnecting from %s:%s"%( self['tpl_host'], self['tpl_port']))

        # self.send('DISCONNECT')
        self.sock.close()

    @lock
    def getNextID(self):
        ocmid = self.next_command_id
        self.next_command_id+=1
        return ocmid

    def sendcomm(self, comm, object):

        cmd = Command()
        cmd.id = self.getNextID()
        cmd.cmd = comm
        cmd.object = object

        status = self.send(cmd)

        if status == SEND.OK:
            self.commands_sent[cmd.id] = cmd
        else:
            return cmd.id

        if comm in ('GET', 'SET'):
            self.commands_sent[cmd.id].data = False

        return cmd.id

    def send(self, message='\r\n'):

        self.log.debug( message )

        try:
            self.sock.write(message)
        except Exception, e:
            self.log.error(e)
            return SEND.ERROR

        return SEND.OK


    def get(self, object, wait=False):

        ret = self.sendcomm('GET', object)

        if wait:
            start = time.time()
            while not self.commands_sent[ret].complete:
                if time.time() > start+self['timeout']:
                    self.log.warning('Command %i timed out...'%(ret))
                    break
                continue

        return ret

    def set(self, object, value, wait=False, binary=False):

        if not binary:
            obj = object + '=' + str(value)
            cmid = self.sendcomm('SET', obj, wait)
        else:
            obj = object + ':', len(value)
            cmid = self.sendcomm('SET', obj, wait)
            self.sock.write(value.tostring())
        if wait:
            start = time.time()
            while not self.commands_sent[cmid].complete:
                if  time.time() > start+self['timeout']:
                    self.log.warning('Command %i timed out...'%(ret))
                    break
                continue

        return cmid


