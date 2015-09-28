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
import numpy as np
import telnetlib
from collections import defaultdict
import re
import shutil
from chimera.core.chimeraobject import ChimeraObject
from chimera.core.lock import lock
from chimera.core.constants import SYSTEM_CONFIG_DIRECTORY
from chimera.core.exceptions import ChimeraException
from chimera.util.enum import Enum

import logging

__all__ = ["TPLBase"]


class TPLException(ChimeraException):
    pass


TPLStatus = Enum("CONNECTED", "CLOSED")

CMDStatus = Enum("DONE","ABORTED","WAITING","TIMEOUT")

SEND = Enum("OK","ERROR")

def retStr():
    return str

_CmdType = defaultdict(retStr)

_CmdType['0'] = str
_CmdType['1'] = int
_CmdType['2'] = float
_CmdType['3'] = str

class Command():

    def __init__(self):
        self.id = 0
        self.cmd = None
        self.object = None
        self.received = []
        self.events = []
        self.dtype = str
        self.status = None
        self.allstatus = []
        self.ok = False
        self.complete = False
        self.data = []


    def __str__(self):
        return str(self.id) + ' ' + self.cmd + ' ' + self.object + '\r\n'

class TPL(ChimeraObject):

    __config__ = {"device": '/dev/ttyS0',
                  "tpl_host": 'localhost',
                  "tpl_port": 65432,
                  "user": 'admin',
                  "password": 'admin',
                  "freq": 2.,
                  "timeout": 60,
                  "waittime": 0.5,
                  "history" : 1000}

    def __init__(self):

        ChimeraObject.__init__(self)

        # debug log
        # self._debugLog = None
        self._debuglog = logging.getLogger('_tpldebug_')
        logfile = os.path.join(SYSTEM_CONFIG_DIRECTORY, "tpl.log")
        if os.path.exists(logfile):
            shutil.move(logfile, os.path.join(SYSTEM_CONFIG_DIRECTORY,
                                              "tpl.log_%s"%time.strftime("%Y%m%d-%H%M%S")))

        _log_handler = logging.FileHandler(logfile)
        _log_handler.setFormatter(logging.Formatter(fmt='%(asctime)s[%(levelname)s:%(threadName)s]-%(name)s-(%(filename)s:%(lineno)d):: %(message)s'))
        # _log_handler.setLevel(logging.DEBUG)
        self._debuglog.setLevel(logging.DEBUG)
        self._debuglog.addHandler(_log_handler)
        self.log.setLevel(logging.INFO)

        # Command counter
        self.next_command_id = 1
        self.last_cmd_deleted = 0

        # Store received objects
        self.commands_sent = {}

        self._expect = [ '(?P<CMDID>\d+) DATA INLINE (?P<OBJECT>\S+)=(?P<VALUE>.+)',
                         '(?P<CMDID>\d+) DATA OK (?P<OBJECT>\S+)',
                         '(?P<CMDID>\d+) COMMAND (?P<STATUS>\S+)',
                         '(?P<CMDID>\d+) EVENT ERROR (?P<OBJECT>\S+):(?P<ENCM>(.*?)\s*)']


    def __start__(self):

        self.setHz(self['freq'])

        self._debuglog.debug('tpl START')
        self.open()

        return True

    def __stop__(self):
        self._debuglog.debug('tpl STOP')
        self.close()

    @lock
    def control(self):

        # self._debuglog.debug('[control] entering...')

        # check if there is any incomplete command
        incomplete = np.any(np.array([not cmd.complete for cmd in self.commands_sent.values()]))
        if incomplete:
            self._debuglog.debug('[control] TPL has incomplete commands')
            for cmd in self.commands_sent.values():
                if not cmd.complete:
                    self._debuglog.debug('[control] Command %i not complete'%cmd.id)
        else:
            return True

        exp_recv = self.expect()
        self._debuglog.debug('[control] Received %i commands'%len(exp_recv))

        for i in range(len(exp_recv)):
            recv = exp_recv[i]

            self._debuglog.debug(recv[2])
            cmdid = int(recv[1].group('CMDID'))
            if not cmdid in self.commands_sent.keys():
                self.log.warning('Received a bad command id %i. Skipping'%cmdid)
                continue

            self.commands_sent[cmdid].received.append(recv[2])

            try:
                if 'DATA INLINE' in recv[2]:
                    if '!TYPE' in recv[2]:
                        self.commands_sent[cmdid].dtype = _CmdType[recv[1].group('VALUE')]
                    else:
                        self.commands_sent[cmdid].data.append(self.commands_sent[cmdid].dtype(recv[1].group('VALUE').replace('"','')))
                elif 'COMMAND' in recv[2]:
                    self.commands_sent[cmdid].status = recv[1].group('STATUS')
                    self.commands_sent[cmdid].allstatus.append(recv[1].group('STATUS'))
                    if self.commands_sent[cmdid].status == 'OK':
                        self.commands_sent[cmdid].ok = True
                    elif self.commands_sent[cmdid].status == 'COMPLETE':
                        self.commands_sent[cmdid].complete = True

                elif 'EVENT ERROR' in recv[2]:
                    self.commands_sent[cmdid].events.append(recv[1].group('ENCM'))

            except Exception,e:
                self.log.error('[control] Error on command: %s'%(recv[2][:-1]))
                self.commands_sent[cmdid].ok = False
                self.commands_sent[cmdid].complete = True
                self.log.exception(e)
                pass

            # incomplete = np.any(np.array([not cmd.complete for cmd in self.commands_sent.values()]))

        # Check size of commands and clear history
        while len(self.commands_sent) > int(self["history"]):
            self.last_cmd_deleted += 1
            self._debuglog.debug('[control] Cleaning command history. Deleting cmd with id: %i'%self.last_cmd_deleted)
            self.commands_sent.pop(self.last_cmd_deleted)

        # self._debuglog.debug('[control] Received %i commands'%nrec)
        # for cmd in self.commands_sent.values():
        #     msg = '%s %s %s'%(cmd.id,cmd.status,cmd.allstatus)
        #     self._debuglog.debug(msg)
        self._debuglog.debug('[control] Done')

        return True

    def expect(self):

        buff = ''
        recv = None
        while recv != '':
            recv = self.sock.read_very_eager()
            buff+=recv

        buff = buff.split('\n')
        ret = []

        for line in buff:

            if len(line) < 1:
                continue
            for exp in self._expect:
                re_exp = re.search(exp,line)
                if re_exp:
                    ret.append((0,re_exp,line))
                    break

        return ret
        #return buff
        #return self.sock.expect(self._expect,
        #                        timeout=self['timeout'])

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

    def getCmd(self,cmdid):
        if cmdid in self.commands_sent.keys():
            return self.commands_sent[cmdid]
        else:
            self.log.warning('cmdid %s does not exists.'%cmdid)
            return None

    def sendcomm(self, comm, object):

        cmd = Command()
        cmd.id = self.getNextID()
        cmd.cmd = comm
        cmd.object = object
        cmd.data = []
        cmd.allstatus = []

        self.commands_sent[cmd.id] = cmd
        status = self.send(cmd)

        if status != SEND.OK:
            self.commands_sent[cmd.id].status = status
            return cmd.id

        # if comm in ('GET', 'SET'):
        #     self.commands_sent[cmd.id].data = False

        return cmd.id

    def send(self, message='\r\n'):

        msg = '%s'%(message)
        self._debuglog.debug( msg[:-1] )

        try:
            self.sock.write('%s'%message)
        except Exception, e:
            self.log.exception(e)
            self.log.warning('Reseting connection...')
            self.close()
            self.open()
            try:
                self.sock.write('%s'%message)
            except Exception, e:
                self.log.exception(e)
                return SEND.ERROR

        return SEND.OK


    def get(self, object, wait=False):

        ret = self.sendcomm('GET', object)

        if wait:
            start = time.time()
            while self.commands_sent[ret].status != 'COMPLETE':
                if time.time() > start+self['timeout']:
                    self.log.warning('Command %i timed out...'%(ret))
                    break
                continue

        return ret

    def set(self, object, value, wait=False, binary=False):

        cmid = None

        if not binary:
            obj = object + '=' + str(value)
            cmid = self.sendcomm('SET', obj)
        else:
            obj = object + ':', len(value)
            cmid = self.sendcomm('SET', obj)
            self.sock.write(value.tostring())
        if wait:
            start = time.time()
            while not self.commands_sent[cmid].status == "COMPLETE":
                if  time.time() > start+self['timeout']:
                    self.log.warning('Command %i timed out...'%(cmid))
                    break
                continue

        return cmid


    def getobject(self, object):

        # ocmid = self.get(object + '!TYPE', wait=True)
        #
        # st = self.commands_sent[ocmid]['status']
        # ntries = 0
        #
        # return None
        #
        # while not st == 'COMPLETE':
        #     log.log(5, '[%3i/%i] TPL2 getobject: got status "%s"'%(ntries,self.max_tries,st) )
        #     ntries+=1
        #     time.sleep(self.sleep)
        #     st = self.commands_sent[ocmid]['status']
        #     if ntries > self.max_tries:
        #         break
        #
        # if st != 'COMPLETE':
        #     log.warning( 'TPL2 getobject: got status %s ...' %st)
        #     return None

        ocmid = self.get(object + '!TYPE;' + object, wait=True)

        start = time.time()
        while not self.commands_sent[ocmid].complete:
            time.sleep(self["waittime"])
            if time.time() > start+self["timeout"]:
                break

        if len(self.commands_sent[ocmid].data) > 0:
            return self.commands_sent[ocmid].data[0]
        else:
            cmd = '%s'%self.commands_sent[ocmid]
            self.log.warning('Command %s returned nothing...'%(cmd[:-2]))
            return None

        st = self.commands_sent[ocmid].status

        while not st == 'COMPLETE':
            log.log(5, '[%3i/%i] TPL2 getobject: got status "%s"'%(ntries,self.max_tries,st) )
            ntries+=1
            time.sleep(self.sleep)
            st = self.commands_sent[ocmid]['status']
            if ntries > self.max_tries:
                break

        if st != 'COMPLETE':
            log.warning( 'TPL2 getobject: got status  %s ...' %st )
            return None
        if self.debug:
            log.log(5,self.received_objects)
        if self.received_objects[object + '!TYPE'] == '0':
            self.received_objects[object] = None
        elif self.received_objects[object + '!TYPE'] == '1':
            self.received_objects[object] = int(self.received_objects[object])
        elif self.received_objects[object + '!TYPE'] == '2':
            self.received_objects[object] = float(
                self.received_objects[object])
        elif self.received_objects[object + '!TYPE'] == '3':
            self.received_objects[object] = str(self.received_objects[object])
        else:
            self.received_objects[object] = None
        return self.received_objects[object]

    def succeeded(self,cmdid):
         return self.commands_sent[cmdid].status == 'COMPLETE'