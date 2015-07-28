#! /usr/bin/env python
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import os
import time
import threading

from chimera.interfaces.focuser import (InvalidFocusPositionException,
                                        FocuserFeature)

from chimera.instruments.focuser import FocuserBase

from chimera.core.lock import lock
from chimera.core.exceptions import ObjectNotFoundException, ChimeraException
from chimera.core.constants import SYSTEM_CONFIG_DIRECTORY

from chimera.util.enum import Enum

class AstelcoException(ChimeraException):
    pass


class AstelcoHexapodException(ChimeraException):
    pass


Direction = Enum("IN", "OUT")
Axis = Enum("X", "Y", "Z", "U", "V")  # For hexapod


class AstelcoFocuser(FocuserBase):
    '''
AstelcoFocuser interfaces chimera with TSI system to control focus. System 
can be equiped with hexapod hardware. In this case, comunition is done in a 
vector. Temperature compensation can also be performed.
    '''

    __config__ = {'hexapod': True,
                  'naxis': 5,
                  'step': 0.001,
                  'unit': 'mm',
                  'tpl': '/TPL/0',
                  'maxidletime': 90.,
                  'model': 'AstelcoFocuser'}  # sec.

    def __init__(self):
        FocuserBase.__init__(self)

        self._supports = {FocuserFeature.TEMPERATURE_COMPENSATION: False,
                          FocuserFeature.POSITION_FEEDBACK: True,
                          FocuserFeature.ENCODER: True}

        self._position = [0] * self['naxis']
        self._range = [None] * self['naxis']
        self._step = [None] * self['naxis']
        self._lastTimeLog = None

        self._abort = threading.Event()

        self._errorNo = 0
        self._errorString = ""

        # debug log
        self._debugLog = None
        try:
            self._debugLog = open(os.path.join(SYSTEM_CONFIG_DIRECTORY,
                                               "astelcofocuser-debug.log"), "w")
        except IOError, e:
            self.log.warning("Could not create astelco debug file (%s)" % str(e))

    def __start__(self):

        self.open()

        tpl = self.getTPL()
        # range and step setting
        if self['hexapod']:
            for ax in Axis:
                min = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].OFFSET!MIN' % ax.index)
                max = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].OFFSET!MAX' % ax.index)
                # step = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].STEP' % ax.index)
                try:
                    min = int(min)
                except:
                    min = -999
                try:
                    max = int(max)
                except:
                    max = 999

                self._range[ax.index] = (min, max)
                self._step[ax.index] = self["step"]
        else:
            min = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS.CURRPOS!MIN')
            max = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS.CURRPOS!MAX')
            self._range[Axis.Z.index] = (min, max)
            self._step[Axis.Z.index] = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS.STEP')

        self.setHz(1. / self["maxidletime"])

        return True

    def __stop__(self):
        self.close()

    @lock
    def control(self):
        '''
        Just keep the connection alive. Everything else is done by astelco.

        :return: True
        '''

        tpl = self.getTPL()
        self.log.debug('[control] %s' % tpl.getobject('SERVER.UPTIME'))

        return True

    def naxis(self):
        return len(self._position)

    @lock
    def open(self):  # converted to Astelco

        try:
            tpl = self.getTPL()
            self.log.debug(tpl.getobject('SERVER.UPTIME'))
            return True

        except:
            raise AstelcoException("Error while opening %s." % self["device"])

    @lock
    def close(self):  # converted to Astelco
        return True

    @lock
    def moveIn(self, n, axis='Z'):
        ax = self.getAxis(axis)
        target = self.getOffset()[ax.index] - n * self._step[ax.index]

        self.log.debug('Setting offset on %s-axis to %f %s ...' % (ax, target, self['unit']))

        if self._inRange(target, ax):
            self._setPosition(target, ax)
        else:
            raise InvalidFocusPositionException("%d is outside focuser "
                                                "boundaries." % target)

    @lock
    def moveOut(self, n, axis='Z'):
        ax = self.getAxis(axis)

        target = self.getOffset()[ax.index] + n * self._step[ax.index]

        self.log.debug('Setting offset on %s-axis to %f %s ...' % (ax, target, self['unit']))

        if self._inRange(target, ax):
            self._setPosition(target, ax)
        else:
            raise InvalidFocusPositionException("%d is outside focuser "
                                                "boundaries." % target)

    @lock
    def moveTo(self, position, axis='Z'):
        ax = self.getAxis(axis)

        self.log.debug('Setting offset on %s-axis to %f %s ...' % (ax, position * self._step[ax.index], self['unit']))

        #return 0

        if self._inRange(position * self._step[ax.index], ax):
            self._setPosition(position * self._step[ax.index], ax)
        else:
            raise InvalidFocusPositionException("%f %s is outside focuser "
                                                "boundaries." % (position * self._step[ax.index],
                                                                 self["unit"]))

    @lock
    def getOffset(self):

        tpl = self.getTPL()
        if self['hexapod']:
            pos = [0] * self['naxis']
            for iax in range(self['naxis']):
                pos[iax] = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].OFFSET' % iax)
            return pos
        else:
            return tpl.getobject('POSITION.INSTRUMENTAL.FOCUS.OFFSET')


    @lock
    def getPosition(self):

        return self.getOffset()[Axis.Z.index]


    def getRange(self, axis='Z'):
        return self._range[self.getAxis(axis).index]

    def _setPosition(self, n, axis=Axis.Z):
        self.log.info("Changing focuser offset to %s" % n)

        cmdid = None
        tpl = self.getTPL()

        if self['hexapod']:
            cmdid = tpl.set('POSITION.INSTRUMENTAL.FOCUS[%i].OFFSET' % axis.index, n)
        else:
            cmdid = tpl.set('POSITION.INSTRUMENTAL.FOCUS.OFFSET', n)

        if not cmdid:
            msg = "Could not change focus offset to %f %s" % (position * self._step[ax.index],
                                                              self["unit"])
            self.log.error(msg)
            raise InvalidFocusPositionException(msg)

        # check limit state
        LSTATE = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].LIMIT_STATE' % axis.index)
        #code = '%16s'%(bin(LSTATE)[2:][::-1])
        bitcode = [0, 1, 7, 8, 9, 15]
        LMESSG = ['MINIMUM HARDWARE LIMIT',
                  'MAXIMUM HARDWARE LIMIT',
                  'HARDWARE BLOCK',
                  'MINIMUM SOFTWARE LIMIT',
                  'MAXIMUM SOFTWARE LIMIT',
                  'SOFTWARE BLOCK']
        STATE = True
        msg = ''
        for ib, bit in enumerate(bitcode):
            if ( LSTATE & (1 << bit) ) != 0:
                STATE = False
                msg += LMESSG[ib] + '|'

        if not STATE:
            msg = 'LIMIT STATE [%i] REACHED on %s-axis: %s' % (LSTATE,
                                                               axis,
                                                               msg)
            self.log.error(msg)
            raise InvalidFocusPositionException(msg)
            #return -1

        self._position[axis.index] = n

        return 0

    def _inRange(self, n, axis=Axis.Z):
        min_pos, max_pos = self.getRange(axis)
        if not min_pos or not max_pos:
            self.log.warning('Minimum and maximum positions not defined...')
            return True
        return (min_pos <= n <= max_pos)

    def getAxis(self, axis=Axis.Z):

        if type(axis) == str:
            return Axis.fromStr(axis)
        elif type(axis) == int:
            return Axis[axis]
        elif type(axis) == type(Axis.Z):
            return axis
        else:
            ldir = ''
            for i in Axis:
                ldir += str(i)
            raise AstelcoHexapodException('Direction not valid! Try one of %s' % ldir)

    def getMetadata(self, request):
        x, y, z, u, v = self.getPosition()
        return [('FOCUSER', str(self['model']), 'Focuser Model'),
                ('XHEX', x, 'Hexapod x position'),
                ('YHEX', y, 'Hexapod y position'),
                ('FOCUS', z,
                 'Focuser position used for this observation'),
                ('UHEX', u, 'Hexapod u angle'),
                ('VHEX', v, 'Hexapod v angle')]

    # utilitaries
    def getTPL(self):
        try:
            p = self.getManager().getProxy(self['tpl'], lazy=True)
            if not p.ping():
                return False
            else:
                return p
        except ObjectNotFoundException:
            return False
