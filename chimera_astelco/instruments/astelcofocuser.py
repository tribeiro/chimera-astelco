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
import collections
import threading

from chimera.interfaces.focuser import (InvalidFocusPositionException,
                                        FocuserFeature, FocuserAxis, ControllableAxis)

from chimera.instruments.focuser import FocuserBase

from chimera.core.lock import lock
from chimera.core.exceptions import ObjectNotFoundException
from chimera.core.constants import SYSTEM_CONFIG_DIRECTORY

from chimera.util.enum import Enum

from astelcoexceptions import AstelcoException, AstelcoHexapodException

Direction = Enum("IN", "OUT")
Axis = FocuserAxis #Enum("X", "Y", "Z", "U", "V")  # For hexapod
AxisStep = {Axis.X : 'step_x',
            Axis.Y : 'step_y',
            Axis.Z : 'step_z',
            Axis.U : 'step_u',
            Axis.V : 'step_v',
            Axis.W : 'step_w',
            }
AxisUnit = {Axis.X : 'unit_x',
            Axis.Y : 'unit_y',
            Axis.Z : 'unit_z',
            Axis.U : 'unit_u',
            Axis.V : 'unit_v',
            Axis.W : 'unit_w',
            }
FocusPosition = collections.namedtuple('Focus','X Y Z U V')

class AstelcoFocuser(FocuserBase):
    '''
AstelcoFocuser interfaces chimera with TSI system to control focus. System 
can be equiped with hexapod hardware. In this case, comunication is done in a
vector. Temperature compensation can also be performed.
    '''

    __config__ = {'tpl': '/TPL/0',
                  'updatetime': 1., # in seconds
                  'model': 'AstelcoFocuser',

                  'hexapod': True,

                  'step_x': 0.001,
                  'step_y': 0.001,
                  'step_z': 0.001,
                  'step_u': 0.001,
                  'step_v': 0.001,
                  'step_w': 0.001,

                  'unit_x': 'mm',
                  'unit_y': 'mm',
                  'unit_z': 'mm',
                  'unit_u': 'deg',
                  'unit_v': 'deg',
                  'unit_w': 'deg',

                  }

    def __init__(self):
        FocuserBase.__init__(self)

        self._supports = {FocuserFeature.TEMPERATURE_COMPENSATION: False,
                          FocuserFeature.POSITION_FEEDBACK: True,
                          FocuserFeature.ENCODER: True,
                          FocuserFeature.CONTROLLABLE_X: False,
                          FocuserFeature.CONTROLLABLE_Y: False,
                          FocuserFeature.CONTROLLABLE_U: False,
                          FocuserFeature.CONTROLLABLE_V: False,
                          FocuserFeature.CONTROLLABLE_W: False,
                          }

        self._position = {Axis.Z: None}
        self._offset = {Axis.Z: None}
        self._range = {Axis.Z: [None, None]}
        self._step = {Axis.Z: None}
        self._lastTimeLog = None
        self._temperature = 0.

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

            for i in ControllableAxis:
                self._supports[i] = True
                self._position[ControllableAxis[i]] = None
                self._offset[ControllableAxis[i]] = None
                self._rangeControllableAxis[i] = [None, None]
                self._step[ControllableAxis[i]] = float(self[AxisStep[ControllableAxis[i]]])

            for ax in Axis:
                min_ = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].REALPOS!MIN' % ax.index)
                max_ = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].REALPOS!MAX' % ax.index)
                self._position[ax] = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].REALPOS' % ax.index)
                self._offset[ax] = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].OFFSET' % ax.index)

                try:
                    min_ = float(min_)
                except Exception, e:
                    self.log.debug('Could not determine minimum of axis %s:\n %s'%(ax,e))
                    min_ = -999
                try:
                    max_ = float(max_)
                except Exception, e:
                    self.log.debug('Could not determine maximum of axis %s:\n %s'%(ax,e))
                    max_ = 999

                self._range[ax] = (min_, max_)
        else:

            min_ = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS.REALPOS!MIN')
            max_ = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS.REALPOS!MAX')

            self._range[Axis.Z] = (min_, max_)
            self._step[Axis.Z] = float(self[AxisStep[Axis.Z]])
            self._position[Axis.Z] = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS.REALPOS')

        self.setHz(1. / self["updatetime"])

        return True

    @lock
    def control(self):
        '''
        Constantly update focuser positions.

        :return: True
        '''

        self.updatePosition()
        self.updateTemperature()

        return True

    @lock
    def moveIn(self, n, axis=FocuserAxis.Z):

        target = self.getOffset(axis) - n * self._step[axis]

        self.log.debug('Setting offset on %s-axis to %f %s ...' % (axis, target, self[AxisUnit[axis]]))

        if self._inRange(target, axis):
            self._setPosition(target, axis)
        else:
            raise InvalidFocusPositionException("%d is outside focuser "
                                                "boundaries." % target)

    @lock
    def moveOut(self, n, axis=FocuserAxis.Z):

        target = self.getOffset(axis) + n * self._step[axis]

        self.log.debug('Setting offset on %s-axis to %f %s ...' % (axis, target, self[AxisUnit[axis]]))

        if self._inRange(target, axis):
            self._setPosition(target, axis)
        else:
            raise InvalidFocusPositionException("%d is outside focuser "
                                                "boundaries." % target)

    @lock
    def moveTo(self, position, axis=FocuserAxis.Z):

        self.log.debug('Setting offset on %s-axis to %f %s ...' % (axis,
                                                                   position * self._step[axis],
                                                                   self[AxisUnit[axis]]))

        if self._inRange(position * self._step[axis], axis):
            self._setPosition(position * self._step[axis], axis)
        else:
            raise InvalidFocusPositionException("%f %s is outside focuser "
                                                "boundaries." % (position * self._step[axis],
                                                                 self[AxisUnit[axis]]))

    def getPosition(self, axis=FocuserAxis.Z):
        return self._position[axis] # self.getTPL().getobject('POSITION.INSTRUMENTAL.FOCUS[%i].REALPOS' % axis.index)

    def getRange(self, axis=FocuserAxis.Z):
        return self._range[axis]

    def getTemperature(self):
        return self._temperature

    def getMetadata(self, request):

        hdr_ = [('FOCUSER', str(self['model']), 'Focuser Model'),
                ('FOCUS', self.getPosition(Axis.Z),'Focuser position used for this observation'),
                ('ZHEX' , self.getPosition(Axis.Z),'Focuser position used for this observation'),
                ('DZHEX', self.getOffset(Axis.Z),'Focuser offset position used for this observation')]
        if self['hexapod']:
            for ax in ControllableAxis:
                hdr_.append([('%sHEX'%ControllableAxis[ax],
                              self.getPosition(ControllableAxis[ax]),
                              'Focuser position in %s used for this observation'%ControllableAxis[ax]),
                             ('D%sHEX'%ControllableAxis[ax],
                              self.getOffset(ControllableAxis[ax]),
                              'Focuser offset position in %s used for this observation'%ControllableAxis[ax])])
        return hdr_

    # utility functions

    def getOffset(self,axis=Axis.Z):
        return self._offset[axis]
        # tpl = self.getTPL()
        # if self['hexapod']:
        #     return self.getTPL().getobject('POSITION.INSTRUMENTAL.FOCUS[%i].OFFSET' % axis.index)
        # else:
        #     return tpl.getobject('POSITION.INSTRUMENTAL.FOCUS.OFFSET')

    @lock
    def updatePosition(self):
        tpl = self.getTPL()
        for ax in Axis:
            self._position[ax] = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].REALPOS' % ax.index)
            self._offset[ax] = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].OFFSET' % ax.index)

    @lock
    def updateTemperature(self):
        pass


    @lock
    def _setPosition(self, n, axis=Axis.Z):
        self.log.info("Changing focuser offset to %s" % n)

        cmdid = None
        tpl = self.getTPL()

        start = time.time()
        if self['hexapod']:
            cmdid = tpl.set('POSITION.INSTRUMENTAL.FOCUS[%i].OFFSET' % axis.index, n)
        else:
            cmdid = tpl.set('POSITION.INSTRUMENTAL.FOCUS.OFFSET', n)

        if not cmdid:
            msg = "Could not change focus offset to %f %s" % (n * self._step[axis],
                                                              self[AxisUnit[axis]])
            self.log.error(msg)
            raise InvalidFocusPositionException(msg)

        MSTATE = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].MOTION_STATE' % axis.index)
        mbitcode = [0, 1, 2, 3, 4]
        MMESSG = ['Axis is moving',
                  'Trajectory is running',
                  'Movement is blocked',
                  'Axis reached desired position',
                  'Axis moving too fast']
        moving = True
        self._abort.clear()
        cmd = tpl.getCmd(cmdid)
        while moving:
            if cmd.complete:
                moving = False
                break
            MSTATE = tpl.getobject('POSITION.INSTRUMENTAL.FOCUS[%i].MOTION_STATE' % axis.index)
            moving = MSTATE != 0
            state = moving
            msg = ''
            for ib, bit in enumerate(mbitcode):
                if ( MSTATE & (1 << bit) ) != 0:
                    #STATE = False
                    msg += MMESSG[ib] + '|'
            if len(msg) > 0:
                self.log.info(msg)
            if time.time() > start+self["move_timeout"]:
                raise AstelcoHexapodException("Operation timed out.")
            if self._abort.isSet():
                self.log.info('Operation aborted')
                # Todo: abort operation
                break
            cmd = tpl.getCmd(cmdid)
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

        # self._position[axis.index] = n
        self.updatePosition()

        return 0

    def _inRange(self, n, axis=Axis.Z):
        min_pos, max_pos = self.getRange(axis)
        if not min_pos or not max_pos:
            self.log.warning('Minimum and maximum positions not defined...')
            return True
        return min_pos <= n <= max_pos

    def getTPL(self):
        try:
            p = self.getManager().getProxy(self['tpl'], lazy=True)
            if not p.ping():
                return False
            else:
                return p
        except ObjectNotFoundException:
            return False
