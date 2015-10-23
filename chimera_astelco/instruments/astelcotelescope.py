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
import threading
import datetime as dt
# from types import FloatType
import os

import numpy as np
from astropy.table import Table

try:
    import cPickle as pickle
except ImportError:
    import pickle

from chimera.instruments.telescope import TelescopeBase
from chimera.interfaces.telescope import SlewRate, AlignMode, TelescopeStatus

from chimera.util.coord import Coord
from chimera.util.position import Position
from chimera.util.enum import Enum

from chimera.core.lock import lock
from chimera.core.exceptions import ObjectNotFoundException, ObjectTooLowException
from chimera.core.constants import SYSTEM_CONFIG_DIRECTORY

from astelcoexceptions import AstelcoException, AstelcoTelescopeException

Direction = Enum("E", "W", "N", "S")
AstelcoTelescopeStatus = Enum("NoLICENSE",
                              "NoTELESCOPE",
                              "OK",
                              "PANIC",
                              "ERROR",
                              "WARNING",
                              "INFO")

class AstelcoTelescope(TelescopeBase):  # converted to Astelco

    __config__ = {'azimuth180Correct': False,
                  'maxidletime': 90.,
                  'parktimeout': 600.,
                  'sensors': 7,
                  'pointing_model': None,      # The filename of the pointing model. None is leave as is
                  'pointing_model_type': None, # Type of pointing model. None is leave as is. either 0,1 or 2
                  'pointing_setup_orientation': None,
                  'pointing_setup_optimization': None,
                  'tpl':'/TPL/0'}  # TODO: FIX tpl so I can get COUNT on an axis.


    def __init__(self):
        TelescopeBase.__init__(self)

        self._slewRate = None
        self._abort = threading.Event()
        self._slewing = False

        self._errorNo = 0
        self._errorString = ""

        self._lastAlignMode = None
        self._parked = False

        self._target_az = None
        self._target_alt = None

        self._ra = None
        self._dec = None

        self._az = None
        self._alt = None

        # debug log
        self._debugLog = None
        try:
            self._debugLog = open(
                os.path.join(SYSTEM_CONFIG_DIRECTORY, "astelcotelescope-debug.log"), "w")
        except IOError, e:
            self.log.warning("Could not create astelco debug file (%s)" % str(e))

        # how much arcseconds / second for every slew rate
        # and direction
        self._calibration = {}
        self._calibration_time = 5.0
        self._calibrationFile = os.path.join(
            SYSTEM_CONFIG_DIRECTORY, "move_calibration.bin")

        self.sensors = []

        for rate in SlewRate:
            self._calibration[rate] = {}
            for direction in Direction:
                self._calibration[rate][direction] = 1

    # -- ILifeCycle implementation --

    def __start__(self):  # converted to Astelco

        self.setHz(1. / self["maxidletime"])

        self.open()

        # try to read saved calibration data
        if os.path.exists(self._calibrationFile):
            try:
                self._calibration = pickle.loads(
                    open(self._calibrationFile, "r").read())
                self.calibrated = True
            except Exception, e:
                self.log.warning(
                    "Problems reading calibration persisted data (%s)" % e)

        return True

    def __stop__(self):  # converted to Astelco

        # if self.isSlewing():
        #     self.abortSlew()

        return True

    @lock
    def open(self):  # converted to Astelco

        try:

            self._checkAstelco()

            # manualy initialize scope
            if self["skip_init"]:
                self.log.info("Skipping init as requested.")
            else:
                self._initTelescope()

            # Update sensors and position
            self.updateSensors()
            tpl = self.getTPL()
            tpl.set('AUXILIARY.PADDLE.BRIGHTNESS',0.0) # set brightness to zero
            # Loading pointing model
            #'pointing_model_type': None, # Type of pointing model. None is leave as is. either 0,1 or 2

            if self['pointing_model'] is not None:
                pt_model = tpl.getobject('POINTING.MODEL.FILE')
                if pt_model != self['pointing_model']:
                    tpl.set('POINTING.MODEL.FILE',self['pointing_model'])
                if self['pointing_model_type'] is not None:
                    pt_model_type = tpl.getobject('POINTING.MODEL.TYPE')
                    if pt_model_type != self['pointing_model_type']:
                        tpl.set('POINTING.MODEL.TYPE',int(self['pointing_model_type']))
                        cmdid = tpl.set('POINTING.MODEL.CALCULATE',1,wait=False)
                        ptt = tpl.getobject('POINTING.MODEL.TYPE')
                        self.log.debug('MODEL TYPE: %s/%s'%(ptt,self['pointing_model_type']))
                        cmd = tpl.getCmd(cmdid)
                        start = time.time()
                        while not cmd.complete:
                            self.log.debug('Waiting for pointing model calculation...')
                            time.sleep(0.1)
                            if time.time() - start > self["max_slew_time"]:
                                self.log.warning('Pointing model calculation taking too long... Will not wait...')
                                break
                            cmd = tpl.getCmd(cmdid)
                        if cmd.complete:
                            modelinfo = tpl.getobject('POINTING.MODEL.CALCULATE')
                            self.log.info('Pointing model quality: %s'%modelinfo)
            ptm_type = tpl.getobject('POINTING.MODEL.TYPE')
            pt_model = tpl.getobject('POINTING.MODEL.FILE')
            modelinfo = tpl.getobject('POINTING.MODEL.CALCULATE')
            mtype = 'None' if ptm_type == 0 else 'NORMAL' if ptm_type == 1 else "EXTENDED"
            self.log.debug('Pointing model info:\n\tNAME: %s\n\tTYPE: %s\n\tQUALITY: %s.'%(pt_model,mtype,modelinfo))

            # Setting up POINTING
            if self['pointing_setup_orientation'] is not None:
                self.log.debug('Setting pointing orientation to: %s'%self['pointing_setup_orientation'])
                try:
                    tpl.set('POINTING.SETUP.ORIENTATION',int(self['pointing_setup_orientation']))
                except:
                    self.log.warning('Could not set orientation.')
                    pass
            if self['pointing_setup_optimization'] is not None:
                tpl.set('POINTING.SETUP.OPTIMIZATION',self['pointing_setup_optimization'])

            orient = tpl.getobject('POINTING.SETUP.ORIENTATION')
            optim = tpl.getobject('POINTING.SETUP.OPTIMIZATION')

            orient = 'NORMAL' if orient == 0 else 'REVERSE' if orient == 1 else 'AUTOMATIC'
            optim = 'NO OPTIMIZATION' if optim == 0 else 'MAX TRACKING TIME' if optim == 1 else "MIN SLEW TIME"
            self.log.info('Current pointing setup:\n\tORIENTATION: %s\n\tOPTIMIZATION: %s'%(orient,optim))
            # tpl.set('POINTING.SETUP.ORIENTATION',2) # AUTOMATIC SELECTION
            # tpl.set('POINTING.SETUP.OPTIMIZATION',2) # MINIMIZE SLEW TIME

            # self.getRa()
            # self.getDec()
            # self.getAlt()
            # self.getAz()

            return True

        except Exception, e:
            raise AstelcoException("Error while opening %s. Error message:\n%s" % (self["device"],
                                                                                   e))

    @lock
    def control(self):
        '''
        Check for telescope status and try to acknowledge any event. This also
        keeps the connection alive.

        :return: True
        '''

        #self.log.debug('[control] %s'%self._tpl.getobject('SERVER.UPTIME'))

        status = self.getTelescopeStatus()

        if status == AstelcoTelescopeStatus.OK:
            self.log.debug('[control] Status: %s' % status)
            return True
        elif status == AstelcoTelescopeStatus.WARNING or status == AstelcoTelescopeStatus.INFO:
            self.log.info('[control] Got telescope status "%s", trying to acknowledge it... ' % status)
            self.logStatus()
            self.acknowledgeEvents()
        elif status == AstelcoTelescopeStatus.PANIC or status == AstelcoTelescopeStatus.ERROR:
            self.logStatus()
            self.log.error('[control] Telescope in %s mode!' % status)
            # What should be done? Try to acknowledge and if that fails do what?
        else:
            self.logStatus()
            self.log.error('[control] Telescope in %s mode!' % status)
            # return False

        # Update sensor and coordinate information
        self.updateSensors()

        # self.getRa()
        # self.getDec()
        # self.getAlt()
        # self.getAz()

        return True

    # --
    # -- ITelescope implementation

    def _checkAstelco(self):  # converted to Astelco
        align = self.getAlignMode()

        if int(align) < 0:
            raise AstelcoException(
                "Couldn't find a Astelco telescope on '%s'." % self["device"])

        return True

    def _initTelescope(self):  # converted to Astelco
        self.setAlignMode(self["align_mode"])

        # set default slew rate
        self.setSlewRate(self["slew_rate"])

        try:
            site = self.getManager().getProxy("/Site/0")

            self.setLat(site["latitude"])
            self.setLong(site["longitude"])
            self.setLocalTime(dt.datetime.now().time())
            self.setUTCOffset(site.utcoffset())
            self.setDate(dt.date.today())
        except ObjectNotFoundException:
            self.log.warning("Cannot initialize telescope. "
                             "Site object not available. Telescope"
                             " attitude cannot be determined.")

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

    def getPMFile(self):
        '''
        Get Pointing Model file
        :return:
        '''
        return self.getTPL().getobject('POINTING.MODEL.FILE')

    def getPMFileList(self):
        '''
        Get Pointing Model file
        :return:
        '''
        flist = self.getTPL().getobject('POINTING.MODEL.FILE_LIST').split(',')
        return flist

    def getPMType(self):
        '''
        Get Pointing Model Type
        :return:
        '''

        tpl = self.getTPL()
        ptm_type = tpl.getobject('POINTING.MODEL.TYPE')
        mtype = 'None' if ptm_type == 0 else 'NORMAL' if ptm_type == 1 else "EXTENDED"
        return ptm_type,mtype

    def setPMType(self,type):
        '''
        Set Pointing Model Type
        :return:
        '''

        if type in [0,1,2]:
            tpl = self.getTPL()
            ptm_type = tpl.getobject('POINTING.MODEL.TYPE')
            self['pointing_model_type'] = int(type)
            tpl.set('POINTING.MODEL.TYPE',int(type))
            return True
        else:
            return False

    def getPMQuality(self):
        '''
        Get Pointing Model Quality
        :return:
        '''

        return self.getTPL().getobject('POINTING.MODEL.CALCULATE')

    def listPM(self):
        'List of all measurements currently in memory.'
        data = self.getTPL().getobject('POINTING.MODEL.LIST').split(';')

        if len(data) == 1:
            return []

        data = [tuple(d.split(',')) for d in data]
        tdata = Table(rows=data,
                      names=('id','name','AZ','dAZ','ZD','dZD','ROT','dROT','DOMEAZ','dDOMEAZ'))
        # dtype = [('id',np.int), ('name','S%i'%np.max([len(line[1]) for line in data])),
        #          ('AZ',np.float),('dAZ',np.float),
        #          ('ZD',np.float),('dZD',np.float),
        #          ('ROT',np.float),('dROT',np.float),
        #          ('DOME',np.float),('dDOME',np.float)]

        return tdata

    def calculatePM(self,mode=1):
        if mode == 1 or mode == 2:
            self.getTPL().set('POINTING.MODEL.CALCULATE',mode,wait=True)
            return True
        else:
            raise AstelcoException('Mode is either 1 (calculate) or 2 (calculate and set offsets to zero).')


    def loadPMFile(self,filename,overwrite):

        flist = self.getPMFileList()
        if filename not in flist:
            return False
        else:
            tpl = self.getTPL()
            self['pointing_model'] = filename
            tpl.set('POINTING.MODEL.FILE',filename)
            tpl.set('POINTING.MODEL.LOAD',1 if overwrite else 2)
            return True

    def clearPMList(self):
        self.getTPL().set('POINTING.MODEL.CLEAR',1)

    def addPM(self,name=""):
        tpl = self.getTPL()
        cmdid = tpl.set('POINTING.MODEL.ADD',name)
        cmd = tpl.getCmd(cmdid)
        return cmd.ok

    def getPSOrientation(self):
        '''
        Get Pointing Setup Orientation
        :return:
        '''
        tpl = self.getTPL()
        orient_id = tpl.getobject('POINTING.SETUP.ORIENTATION')
        orient = 'NORMAL' if orient_id == 0 else 'REVERSE' if orient_id == 1 else 'AUTOMATIC'
        return orient_id if orient_id in [0,1,2] else 2,orient

    def setPSOrientation(self,orientation):
        tpl = self.getTPL()
        try:
            orient = int(orientation)
            # set to automatic if out of range
            orient = orient if orient in [0,1,2] else 2
            if orient != tpl.getobject('POINTING.SETUP.ORIENTATION'):
                tpl.set('POINTING.SETUP.ORIENTATION',orient)
                self['pointing_setup_orientation'] = orient
        except Exception,e:
            self.log.exception(e)
            pass

    def getPSOptimization(self):
        '''
        Get Pointing Setup Optimization
        :return:
        '''
        tpl = self.getTPL()
        optim_id = tpl.getobject('POINTING.SETUP.OPTIMIZATION')
        optim = 'NO OPTIMIZATION' if optim_id == 0 else 'MAX TRACKING TIME' if optim_id == 1 else "MIN SLEW TIME"

        return optim_id,optim


    @lock
    def autoAlign(self):  # converted to Astelco

        return True

    @lock
    def getAlignMode(self):  # converted to Astelco

        tpl = self.getTPL()

        ret = tpl.getobject('TELESCOPE.CONFIG.MOUNTOPTIONS')

        if not ret or ret not in ("AZ-ZD", "ZD-ZD", "HA-DEC"):
            raise AstelcoException(
                "Couldn't get the alignment mode. Is this an Astelco??")

        if ret == "AZ-ZD":
            return AlignMode.ALT_AZ
        elif ret == "HA-DEC":
            return AlignMode.LAND
        else:
            return None

    @lock
    def setAlignMode(self, mode):  # converted to Astelco

        if mode == self.getAlignMode():
            return True
        else:
            return False

    @lock
    def slewToRaDec(self, position):  # no need to convert to Astelco
        self.log.debug('Validating position')
        self._validateRaDec(position)
        self.log.debug('Ok')

        self.log.debug("Check if telescope is already slewing")
        if self.isSlewing():
            # never should happens 'cause @lock
            raise AstelcoException("Telescope already slewing.")
        self.log.debug("OK")

        self.log.debug('Setting target RA/DEC')
        self.setTargetRaDec(position.ra, position.dec)
        self.log.debug('Done')

        status = TelescopeStatus.OK

        try:
            status = self._slewToRaDec()
            #return True
        except Exception, e:
            self._slewing = False
            if self._abort.isSet():
                status = TelescopeStatus.ABORTED
            else:
                status = TelescopeStatus.ERROR
            self.slewComplete(self.getPositionRaDec(), status)
            self.log.exception(e)
        finally:
            self._slewing = False
            self.slewComplete(self.getPositionRaDec(), status)
            return status


    def _slewToRaDec(self):  # converted to Astelco
        self._slewing = True
        self._abort.clear()

        tpl = self.getTPL()
        # slew
        slewTime = tpl.getobject('POINTING.SLEWTIME')
        self.log.info("Time to slew to RA/Dec is reported to be %f s" % ( slewTime ))

        target = self.getTargetRaDec()

        status = self._waitSlew(time.time(), target, slew_time=slewTime)

        if status == TelescopeStatus.OK:
            return self._startTracking(time.time(), target, slew_time=slewTime)
        else:
            return TelescopeStatus.ERROR

    @lock
    def slewToAltAz(self, position):  # no need to convert to Astelco
        self._validateAltAz(position)

        # self.setSlewRate(self["slew_rate"])

        if self.isSlewing():
            # never should happens 'cause @lock
            raise AstelcoException("Telescope already slewing.")

        self.setTargetAltAz(position.alt, position.az)

        status = TelescopeStatus.OK

        try:
            status = self._slewToAltAz()
            #return True
        except Exception, e:
            self._slewing = False
            status = TelescopeStatus.ERROR
            if self._abort.isSet():
                status = TelescopeStatus.ABORTED
        finally:
            self.slewComplete(self.getPositionRaDec(), status)
            return status

    def _slewToAltAz(self):  # converted to Astelco
        self._slewing = True
        self._abort.clear()

        tpl = self.getTPL()
        slewTime = tpl.getobject('POINTING.SLEWTIME')

        self.log.debug("Time to slew to Alt/Az is reported to be %s s." % slewTime)

        target = self.getTargetAltAz()
        self.log.debug("Target Alt/Az  %s s." % target)

        # return TelescopeStatus.OK
        return self._waitSlew(time.time(), target, local=True)

    def _waitSlew(self, start_time, target, local=False, slew_time=-1):  # converted to Astelco
        self.slewBegin(target)
        # todo: raise an exception if telescope is parked
        tpl = self.getTPL()
        # Set offset to zero
        if abs(self._getOffset(Direction.N)) > 0:
            cmdid = tpl.set('POSITION.INSTRUMENTAL.DEC.OFFSET', 0.0, wait=True)
            # time.sleep(self["stabilization_time"])
        if abs(self._getOffset(Direction.W)) > 0:
            cmdid = tpl.set('POSITION.INSTRUMENTAL.HA.OFFSET', 0.0, wait=True)
            # time.sleep(self["stabilization_time"])

        self.log.debug('SEND: POINTING.TRACK 2')
        cmdid = tpl.set('POINTING.TRACK', 2, wait=False)
        self.log.debug('PASSED')

        cmd = tpl.getCmd(cmdid)

        # time_sent = time.time()
        while not cmd.complete:

            if not self.checkLimits():
                return TelescopeStatus.ABORTED

            if self._abort.isSet():
                self._slewing = False
                self.stopMoveAll()
                self.slewComplete(self.getPositionRaDec(),
                    TelescopeStatus.ABORTED)
                return TelescopeStatus.ABORTED

            # check timeout
            if time.time() >= (start_time + self["max_slew_time"]):
                self.stopMoveAll()
                self._slewing = False
                self.log.error('Slew aborted. Max slew time reached.')
                raise AstelcoException("Slew aborted. Max slew time reached.")

            if time.time() >= (start_time + slew_time):
                self.log.warning('Estimated slewtime has passed...')
                position = self.getPositionRaDec()
                if local:
                    position = self.getPositionAltAz()
                angsep = target.angsep(position)
                self.log.debug('Target: %s | Position: %s | Distance: %s' % (target, position, angsep))

                slew_time += slew_time

            # time.sleep(self["slew_idle_time"])
            cmd = tpl.getCmd(cmdid)

        #     time.sleep(self["slew_idle_time"])
        #     if time.time() > time_sent+self["max_slew_time"]:
        #         break
        #
        # err = not cmd.ok
        #
        # if err:
        #     # check error message
        #     msg = cmd.received
        #     self.log.error('Error pointing to %s' % target)
        #     for line in msg:
        #         self.log.error(line)
        #     self.slewComplete(self.getPositionRaDec(),
        #         TelescopeStatus.ERROR)
        #
        #     return TelescopeStatus.ERROR

        # self.log.debug('Wait cmd complete...')
        # status = self.waitCmd(cmdid, start_time, slew_time)
        # self.log.debug('Done')

        # if status != TelescopeStatus.OK:
        #     self.log.warning('Pointing operations failed with status: %s...' % status)
        #     self.slewComplete(self.getPositionRaDec(),
        #         status)
        #     return status

        # self.log.debug('Wait movement start...')
        # time.sleep(self["stabilization_time"])

        self.log.debug('Wait slew to complete...')

        while True:

            if not self.checkLimits():
                return TelescopeStatus.ABORTED

            if self._abort.isSet():
                self._slewing = False
                self.abortSlew()
                self.slewComplete(self.getPositionRaDec(),
                    TelescopeStatus.ABORTED)
                return TelescopeStatus.ABORTED

            # check timeout
            if time.time() >= (start_time + self["max_slew_time"]):
                self.stopMoveAll()
                self._slewing = False
                self.log.error('Slew aborted. Max slew time reached.')
                raise AstelcoException("Slew aborted. Max slew time reached.")

            if time.time() >= (start_time + slew_time):
                self.log.warning('Estimated slewtime has passed...')
                position = self.getPositionRaDec()
                if local:
                    position = self.getPositionAltAz()
                angsep = target.angsep(position)
                self.log.debug('Target: %s | Position: %s | Distance: %f' % (target, position, angsep.AS))
                if angsep.AS < 60.:
                    self.abortSlew()

                slew_time += slew_time

            dec_state = tpl.getobject('POSITION.INSTRUMENTAL.DEC.MOTION_STATE')
            ha_state = tpl.getobject('POSITION.INSTRUMENTAL.DEC.MOTION_STATE')

            mstate = tpl.getobject('TELESCOPE.MOTION_STATE')

            self.log.debug('MSTATE: %i (%s) dec= %s ra=%s' % (mstate, bin(mstate),bin(dec_state),bin(ha_state)))
            if (mstate & 1) == 0:
                self.log.debug('Slew finished...')
                return TelescopeStatus.OK

            # time.sleep(self["slew_idle_time"])
            cmd = tpl.getCmd(cmdid)

    def _startTracking(self, start_time, target, local=False, slew_time=-1):  # converted to Astelco):

        tpl = self.getTPL()
        self.log.debug('SEND: POINTING.TRACK 1')
        cmdid = tpl.set('POINTING.TRACK', 1, wait=True)
        self.log.debug('PASSED')

        cmd = tpl.getCmd(cmdid)

        self.log.debug('Wait for telescope to stabilize...')
        # time.sleep(self["stabilization_time"])

        # self.log.debug('Wait cmd complete...')
        # status = self.waitCmd(cmdid, start_time, slew_time)
        # self.log.debug('Done')

        # self.log.debug('Wait slew to complete...')

        # time.sleep(self["slew_idle_time"])

        while not cmd.complete:
            
            if not self.checkLimits():
                return TelescopeStatus.ABORTED

            if self._abort.isSet():
                self._slewing = False
                self.abortSlew()
                self.slewComplete(self.getPositionRaDec(),
                    TelescopeStatus.ABORTED)
                return TelescopeStatus.ABORTED

            # check timeout
            if time.time() >= (start_time + self["max_slew_time"]):
                self.abortSlew()
                self._slewing = False
                self.log.error('Slew aborted. Max slew time reached.')
                raise AstelcoException("Slew aborted. Max slew time reached.")

            if time.time() >= (start_time + slew_time):
                self.log.warning('Estimated slewtime has passed...')
                slew_time += slew_time

            # time.sleep(self["slew_idle_time"])
            cmd = tpl.getCmd(cmdid)

        # self.log.debug('Wait for telescope to stabilize...')
        # time.sleep(self["stabilization_time"])

        # no need to check it here...
        return TelescopeStatus.OK


    # def waitCmd(self, cmdid, start_time, op_time=-1):
    #
    #     if op_time < 0:
    #         op_time = self["max_slew_time"] + 1
    #
    #     tpl = self.getTPL()
    #     while not tpl.commands_sent[cmdid].complete:
    #
    #         if self._abort.isSet():
    #             self._slewing = False
    #             self.abortSlew()
    #             self.slewComplete(self.getPositionRaDec(),
    #                 TelescopeStatus.ABORTED)
    #             return TelescopeStatus.ABORTED
    #
    #         # check timeout
    #         if time.time() >= (start_time + self["max_slew_time"]):
    #             self.abortSlew()
    #             self._slewing = False
    #             self.log.error('Slew aborted. Max slew time reached.')
    #             raise AstelcoException("Slew aborted. Max slew time reached.")
    #
    #         if time.time() >= (start_time + op_time):
    #             self.log.warning('Estimated slewtime has passed...')
    #             op_time += op_time
    #
    #         time.sleep(self["slew_idle_time"])
    #
    #     return TelescopeStatus.OK

    def abortSlew(self):  # converted to Astelco
        self.stopMoveAll()


        time.sleep(self["stabilization_time"])

    def isSlewing(self):  # converted to Astelco

        # if this is true, then chimera issue a slewing command
        # if self._slewing:
        #     return self._slewing
        # if not, need to check if a external command did that...

        return self._isSlewing()

    def _isSlewing(self):

        tpl = self.getTPL()
        self.log.debug('GET TELESCOPE.MOTION_STATE')
        mstate = tpl.getobject('TELESCOPE.MOTION_STATE')
        self.log.debug('GET POINTING.TRACK')
        ptrack = tpl.getobject('POINTING.TRACK')
        self.log.debug('Done')

        self._slewing = (int(mstate) != 0) and (int(ptrack) != 1)

        return self._slewing

    def _getOffset(self, direction):

        tpl = self.getTPL()
        if direction == Direction.E or direction == Direction.W:
            return tpl.getobject('POSITION.INSTRUMENTAL.HA.OFFSET')
        elif direction == Direction.N or direction == Direction.S:
            return tpl.getobject('POSITION.INSTRUMENTAL.DEC.OFFSET')
        else:
            return 0

    def _move(self, direction, offset, slewRate=SlewRate.GUIDE):  # yet to convert to Astelco

        if offset / 3600. > 2.0:
            raise AstelcoException("Offset %.2f %s too large!"%(offset.D,direction))

        current_offset = self._getOffset(direction)

        self._slewing = True
        cmdid = 0

        self.log.debug('Current offset: %s | Requested: %s' % (current_offset, offset))

        tpl = self.getTPL()

        if direction == Direction.W:
            off = current_offset - offset / 3600. * np.cos(self.getDec().R)
            cmdid = tpl.set('POSITION.INSTRUMENTAL.HA.OFFSET', off, wait=True)
        elif direction == Direction.E:
            off = current_offset + offset / 3600. * np.cos(self.getDec().R)
            cmdid = tpl.set('POSITION.INSTRUMENTAL.HA.OFFSET', off, wait=True)
        elif direction == Direction.N:
            cmdid = tpl.set('POSITION.INSTRUMENTAL.DEC.OFFSET', current_offset + offset / 3600., wait=True)
        elif direction == Direction.S:
            cmdid = tpl.set('POSITION.INSTRUMENTAL.DEC.OFFSET', current_offset - offset / 3600., wait=True)
        else:
            self._slewing = False
            return True

        # self.log.debug('Wait for telescope to stabilize...')
        # time.sleep(self["stabilization_time"])
        #
        # self.log.debug('Wait cmd complete...')
        # start_time = time.time()
        # slew_time = self["stabilization_time"]
        # # status = self.waitCmd(cmdid, start_time, slew_time)
        #
        # self.log.debug('SEND: POINTING.TRACK 1')
        # cmdid = tpl.set('POINTING.TRACK', 1, wait=False)
        # self.log.debug('PASSED')
        #
        # # self.log.debug('Wait for telescope to stabilize...')
        # # time.sleep(self["stabilization_time"])
        #
        # self.log.debug('Wait cmd completion...')
        # cmd = tpl.getCmd(cmdid)
        #
        # # time_sent = time.time()
        # # while not cmd.complete:
        # status = self._waitSlewLoop(cmdid,start_time,slew_time)
        #     # cmd = tpl.getCmd(cmdid)
        #
        # # status = self.waitCmd(cmdid, start_time, slew_time)
        # # self.log.debug('Done')
        # self._slewing = False
        #
        # if status == TelescopeStatus.OK:
        #     return True
        # else:
        #     return False

        return True

    def _waitSlewLoop(self,cmdid,start_time,slew_time=None):

        tpl = self.getTPL()
        cmd = tpl.getCmd(cmdid)

        while not cmd.complete:

            if self._abort.isSet():
                self._slewing = False
                self.abortSlew()
                self.slewComplete(self.getPositionRaDec(),
                    TelescopeStatus.ABORTED)
                return TelescopeStatus.ABORTED

            # check timeout
            if time.time() >= (start_time + self["max_slew_time"]):
                self.abortSlew()
                self._slewing = False
                self.log.error('Slew aborted. Max slew time reached.')
                raise AstelcoException("Slew aborted. Max slew time reached.")

            if slew_time and time.time() >= (start_time + slew_time):
                self.log.warning('Estimated slewtime has passed...')
                slew_time += slew_time

            # time.sleep(self["slew_idle_time"])
            cmd = tpl.getCmd(cmdid)
        time.sleep(self["stabilization_time"])

        return TelescopeStatus.OK

    def _stopMove(self, direction):

        self.stopMoveAll()

        return True

    def isMoveCalibrated(self):  # no need to convert to Astelco
        return os.path.exists(self._calibrationFile)

    @lock
    def calibrateMove(self):  # no need to convert to Astelco
        # FIXME: move to a safe zone to do calibrations.
        def calcDelta(start, end):
            return end.angsep(start)

        def calibrate(direction, rate):
            start = self.getPositionRaDec()
            self._move(direction, self._calibration_time, rate)
            end = self.getPositionRaDec()

            return calcDelta(start, end)

        for rate in SlewRate:
            for direction in Direction:
                self.log.debug("Calibrating %s %s" % (rate, direction))

                total = 0

                for i in range(2):
                    total += calibrate(direction, rate).AS

                self.log.debug("> %f" % (total / 2.0))
                self._calibration[rate][direction] = total / 2.0

        # save calibration
        try:
            f = open(self._calibrationFile, "w")
            f.write(pickle.dumps(self._calibration))
            f.close()
        except Exception, e:
            self.log.warning("Problems persisting calibration data. (%s)" % e)

        self.log.info("Calibration was OK.")

    def _calcDuration(self, arc, direction, rate):  # no need to convert to Astelco
        """
        Calculates the time spent (returned number) to move by arc in a
        given direction at a given rate
        """

        if not self.isMoveCalibrated():
            self.log.info("Telescope fine movement not calibrated. Calibrating now...")
            self.calibrateMove()

        self.log.debug("[move] asked for %s arcsec" % float(arc))

        return arc * (self._calibration_time / self._calibration[rate][direction])

    @lock
    def moveEast(self, offset, slewRate=None):  # no need to convert to Astelco
        return self._move(Direction.E,
                          offset,
                          slewRate)

    @lock
    def moveWest(self, offset, slewRate=None):  # no need to convert to Astelco
        return self._move(Direction.W,
                          offset,
                          slewRate)

    @lock
    def moveNorth(self, offset, slewRate=None):  # no need to convert to Astelco
        return self._move(Direction.N,
                          offset,
                          slewRate)

    @lock
    def moveSouth(self, offset, slewRate=None):  # no need to convert to Astelco
        return self._move(Direction.S,
                          offset,
                          slewRate)

    @lock
    def stopMoveEast(self):  # no need to convert to Astelco
        return self._stopMove(Direction.E)

    @lock
    def stopMoveWest(self):  # no need to convert to Astelco
        return self._stopMove(Direction.W)

    @lock
    def stopMoveNorth(self):  # no need to convert to Astelco
        return self._stopMove(Direction.N)

    @lock
    def stopMoveSouth(self):  # no need to convert to Astelco
        return self._stopMove(Direction.S)

    @lock
    def stopMoveAll(self):  # converted to Astelco
        tpl = self.getTPL()
        tpl.set('TELESCOPE.STOP', 1, wait=True)
        return True

    @lock
    def _getRa(self):
        if not self._ra:
            return self.getRa()
        return self._ra

    @lock
    def _getDec(self):
        if not self._dec:
            return self.getDec()

        return self._dec

    @lock
    def getRa(self):  # converted to Astelco

        tpl = self.getTPL()
        ret = tpl.getobject('POSITION.EQUATORIAL.RA_J2000')
        if ret:
            self._ra = Coord.fromH(ret)
        self.log.debug('Ra: %s' % ret)
        return self._ra

    @lock
    def getDec(self):  # converted to Astelco
        tpl = self.getTPL()
        ret = tpl.getobject('POSITION.EQUATORIAL.DEC_J2000')
        if ret:
            self._dec = Coord.fromD(ret)
        self.log.debug('Dec: %s' % ret)
        return self._dec

    @lock
    def getPositionRaDec(self):  # no need to convert to Astelco
        return Position.fromRaDec(self.getRa(), self.getDec())

    @lock
    def getPositionAltAz(self):  # no need to convert to Astelco
        return Position.fromAltAz(self.getAlt(), self.getAz())

    @lock
    def getTargetRaDec(self):  # no need to convert to Astelco
        return Position.fromRaDec(self.getTargetRa(), self.getTargetDec())

    @lock
    def getTargetAltAz(self):  # no need to convert to Astelco
        return Position.fromAltAz(self.getTargetAlt(), self.getTargetAz())

    @lock
    def setTargetRaDec(self, ra, dec):  # no need to convert to Astelco
        self.setTargetRa(ra)
        self.setTargetDec(dec)

        return True

    @lock
    def setTargetAltAz(self, alt, az):  # no need to convert to Astelco
        self.setTargetAz(az)
        self.setTargetAlt(alt)

        return True

    @lock
    def getTargetRa(self):  # converted to Astelco
        tpl = self.getTPL()
        ret = tpl.getobject('OBJECT.EQUATORIAL.RA')

        return Coord.fromH(ret)

    @lock
    def setTargetRa(self, ra):  # converted to Astelco
        if not isinstance(ra, Coord):
            ra = Coord.fromHMS(ra)

        tpl = self.getTPL()
        cmdid = tpl.set('OBJECT.EQUATORIAL.RA', ra.H, wait=True)

        ret = tpl.succeeded(cmdid)

        if not ret:
            raise AstelcoException("Invalid RA '%s'" % ra)

        return True

    @lock
    def setTargetDec(self, dec):  # converted to Astelco
        if not isinstance(dec, Coord):
            dec = Coord.fromDMS(dec)

        tpl = self.getTPL()
        cmdid = tpl.set('OBJECT.EQUATORIAL.DEC', dec.D, wait=True)

        ret = tpl.succeeded(cmdid)

        if not ret:
            raise AstelcoException("Invalid DEC '%s'" % dec)

        return True

    @lock
    def getTargetDec(self):  # converted to Astelco
        tpl = self.getTPL()
        ret = tpl.getobject('OBJECT.EQUATORIAL.DEC')

        return Coord.fromD(ret)

    @lock
    def _getAz(self):  # converted to Astelco

        if not self._az:
            return self.getAz()

        c = self._az  #Coord.fromD(ret)

        if self['azimuth180Correct']:
            if c.toD() >= 180:
                c = c - Coord.fromD(180)
            else:
                c = c + Coord.fromD(180)

        return c

    @lock
    def _getAlt(self):  # converted to Astelco
        if not self._alt:
            return self.getAlt()

        return self._alt

    @lock
    def getAz(self):  # converted to Astelco
        tpl = self.getTPL()
        ret = tpl.getobject('POSITION.HORIZONTAL.AZ')
        if ret:
            self._az = Coord.fromD(ret)
        self.log.debug('Az: %s' % ret)

        c = self._az  #Coord.fromD(ret)

        if self['azimuth180Correct']:
            if c.toD() >= 180:
                c = c - Coord.fromD(180)
            else:
                c = c + Coord.fromD(180)

        return c

    @lock
    def getAlt(self):  # converted to Astelco
        tpl = self.getTPL()
        ret = tpl.getobject('POSITION.HORIZONTAL.ALT')
        if ret:
            self._alt = Coord.fromD(ret)
        self.log.debug('Alt: %s' % ret)

        return self._alt

    @lock
    def getParallacticAngle(self):  # converted to Astelco
        tpl = self.getTPL()
        ret = tpl.getobject('POSITION.EQUATORIAL.PARALLACTIC_ANGLE')
        if ret is not None:
            ret = Coord.fromD(ret)
        else:
            ret = Coord.fromD(-1)

        return ret

    def getTargetAlt(self):  # no need to convert to Astelco
        return self._target_alt

    @lock
    def setTargetAlt(self, alt):  # converted to Astelco
        if not isinstance(alt, Coord):
            alt = Coord.fromD(alt)

        tpl = self.getTPL()
        cmdid = tpl.set('OBJECT.HORIZONTAL.ALT', alt.D, wait=True)

        ret = tpl.succeeded(cmdid)

        if not ret:
            raise AstelcoException("Invalid Altitude '%s'" % alt)

        self._target_alt = alt

        return True

    def getTargetAz(self):  # no need to convert to Astelco
        return self._target_az

    @lock
    def setTargetAz(self, az):  # converted to Astelco
        if not isinstance(az, Coord):
            az = Coord.fromDMS(az)

        if self['azimuth180Correct']:

            if az.toD() >= 180:
                az = az - Coord.fromD(180)
            else:
                az = az + Coord.fromD(180)

        tpl = self.getTPL()
        cmdid = tpl.set('OBJECT.HORIZONTAL.AZ', az.D, wait=True)

        ret = tpl.succeeded(cmdid)

        if not ret:
            raise AstelcoException(
                "Invalid Azimuth '%s'" % az.strfcoord("%(d)03d\xdf%(m)02d"))

        self._target_az = az

        return True

    def checkLimits(self):
        alt = self.getAlt()
        try:
            self._validateAltAz(self.getPositionAltAz())
        except ObjectTooLowException,e:
            self.stopMoveAll()
            self.log.exception(e)
            return False
        except:
            pass
        return True


    @lock
    def getLat(self):  # converted to Astelco
        tpl = self.getTPL()
        ret = tpl.getobject('POINTING.SETUP.LOCAL.LATITUDE')

        return Coord.fromD(ret)

    @lock
    def setLat(self, lat):  # converted to Astelco
        if not isinstance(lat, Coord):
            lat = Coord.fromDMS(lat)

        lat_float = float(lat.D)

        tpl = self.getTPL()
        cmdid = tpl.set(
            'POINTING.SETUP.LOCAL.LATITUDE', float(lat_float), wait=True)
        ret = tpl.succeeded(cmdid)
        if not ret:
            raise AstelcoException(
                "Invalid Latitude '%s' ('%s')" % (lat, lat_float))
        return True

    @lock
    def getLong(self):  # converted to Astelco
        tpl = self.getTPL()
        ret = tpl.getobject('POINTING.SETUP.LOCAL.LONGITUDE')
        return Coord.fromD(ret)

    @lock
    def setLong(self, coord):  # converted to Astelco
        if not isinstance(coord, Coord):
            coord = Coord.fromDMS(coord)
        tpl = self.getTPL()

        cmdid = tpl.set(
            'POINTING.SETUP.LOCAL.LONGITUDE', coord.D, wait=True)
        ret = tpl.succeeded(cmdid)
        if not ret:
            raise AstelcoException("Invalid Longitude '%s'" % coord.D)
        return True

    @lock
    def getDate(self):  # converted to Astelco
        tpl = self.getTPL()
        timef = time.mktime(
            time.localtime(tpl.getobject('POSITION.LOCAL.UTC')))
        return dt.datetime.fromtimestamp(timef).date()

    @lock
    def setDate(self, date):  # converted to Astelco
        return True

    @lock
    def getLocalTime(self):  # converted to Astelco
        tpl = self.getTPL()
        timef = time.mktime(
            time.localtime(tpl.getobject('POSITION.LOCAL.UTC')))
        return dt.datetime.fromtimestamp(timef).time()

    @lock
    def setLocalTime(self, local):  # converted to Astelco
        ret = True
        if not ret:
            raise AstelcoException("Invalid local time '%s'." % local)
        return True

    @lock
    def getLocalSiderealTime(self):  # converted to Astelco
        tpl = self.getTPL()
        ret = tpl.getobject('POSITION.LOCAL.SIDEREAL_TIME')
        return Coord.fromH(ret)

    @lock
    def setLocalSiderealTime(self, local):  # converted to Astelco
        return True

    @lock
    def getUTCOffset(self):  # converted to Astelco
        return time.timezone / 3600.0

    @lock
    def setUTCOffset(self, offset):  # converted to Astelco
        ret = True
        if not ret:
            raise AstelcoException("Invalid UTC offset '%s'." % offset)
        return True

    @lock
    def getCurrentTrackingRate(self):  # yet to convert to Astelco
        #self._write(":GT#")
        ret = False  # self._readline()
        if not ret:
            raise AstelcoException("Couldn't get the tracking rate")
        ret = float(ret[:-1])
        return ret

    @lock
    def setCurrentTrackingRate(self, trk):  # yet to convert to Astelco
        trk = "%02.1f" % trk
        if len(trk) == 3:
            trk = "0" + trk
        #self._write(":ST%s#" % trk)
        ret = False  # self._readbool()
        if not ret:
            raise AstelcoException("Invalid tracking rate '%s'." % trk)
        #self._write(":TM#")
        return ret

    @lock
    def startTracking(self):  # converted to Astelco
        tpl = self.getTPL()
        cmdid = tpl.set('POINTING.TRACK', 1, wait=True)
        return tpl.succeeded(cmdid)


    @lock
    def stopTracking(self):  # converted to Astelco
        tpl = self.getTPL()
        cmdid = tpl.set('POINTING.TRACK', 0, wait=True)
        return tpl.succeeded(cmdid)


    def isTracking(self):  # converted to Astelco
        tpl = self.getTPL()
        return tpl.getobject('POINTING.TRACK')


    # -- ITelescopeSync implementation --
    @lock
    def syncRaDec(self, position):  # yet to convert to Astelco
        self.setTargetRaDec(position.ra, position.dec)
        #self._write(":CM#")
        ret = False  # self._readline ()
        if not ret:
            raise AstelcoException(
                "Error syncing on '%s' '%s'." % (position.ra, position.dec))
        self.syncComplete(self.getPositionRaDec())
        return True

    @lock
    def setSlewRate(self, rate):  # no need to convert to Astelco
        self._slewRate = rate
        return True

    def getSlewRate(self):  # no need to convert to Astelco
        return self._slewRate

    # -- park

    def getParkPosition(self):  # no need to convert to Astelco
        return Position.fromAltAz(self["park_position_alt"],
                                  self["park_position_az"])

    @lock
    def setParkPosition(self, position):  # no need to convert to Astelco
        self["park_position_az"], self["park_position_alt"] = position.D

        return True

    def isParked(self):  # (yes) -no- need to convert to Astelco
        tpl = self.getTPL()
        self._parked = tpl.getobject('TELESCOPE.READY_STATE') == 0
        return self._parked

    def isOpen(self):  # (yes) -no- need to convert to Astelco
        tpl = self.getTPL()
        self._open = tpl.getobject('AUXILIARY.COVER.REALPOS') == 1
        return self._open

    @lock
    def park(self):  # converted to Astelco
        if self.isParked():
            return True

        # 1. slew to park position FIXME: allow different park
        # positions and conversions from ra/dec -> az/alt

        site = self.getManager().getProxy("/Site/0")
        #self.slewToRaDec(Position.fromRaDec(str(self.getLocalSiderealTime()),
        #                                            site["latitude"]))
        tpl = self.getTPL()
        cmdid = tpl.set('TELESCOPE.READY', 0, wait=False)

        ready_state = tpl.getobject('TELESCOPE.READY_STATE')
        start_time = time.time()
        self._abort.clear()

        while ready_state > 0.0:
            self.log.debug("Powering down Astelco: %s" % (ready_state))
            old_ready_state = ready_state
            ready_state = tpl.getobject('TELESCOPE.READY_STATE')
            if ready_state != old_ready_state:
                self.log.debug("Powering down Astelco: %s" % (ready_state))
                old_ready_state = ready_state
            if self._abort.set():
                # Send abork command to astelco
                self.log.warning("Abort parking! This will leave the telescope in an intermediate state!")
                tpl.set('ABORT', cmdid)
                return False
            if time.time() > start_time + self['parktimeout']:
                self.log.error("Parking operation timedout!")
                return False
            if self.getTelescopeStatus() != AstelcoTelescopeStatus.OK:
                self.log.warning("Something wrong with telescope! Trying to fix it!")
                self.logStatus()
                self.acknowledgeEvents()
                # What should I do if acknowledging events does not fix it?

            time.sleep(5.0)

        # 2. stop tracking
        #self.stopTracking ()
        # 3. power off
        #self.powerOff ()
        self._parked = True

        self.parkComplete()

        return tpl.succeeded(cmdid)

    def getTelescopeStatus(self):
        '''
        Get telescope status.
        -2 - No valid license found
        -1 - No Telescope hardware found
        0 - Operational
        Bit 0 - PANIC, a severe condition, completely disabling the entire telescope,
        Bit 1 - ERROR, a serious condition, disabling important parts of the telescope system,
        Bit 2 - WARNING, a critical condition, which is not (yet) dis- abling the telescope,
        Bit 3 - INFO, a informal situation, which is not a ecting the operation.

        :return: AstelcoTelescopeStatus{Enum}
        '''
        status = None

        tpl = self.getTPL()

        while not status:
            status = tpl.getobject('TELESCOPE.STATUS.GLOBAL')
            if status == 0:
                return AstelcoTelescopeStatus.OK

        if status == -2:
            return AstelcoTelescopeStatus.NoLICENSE
        elif status == -1:
            return AstelcoTelescopeStatus.NoTELESCOPE
        elif (status & ( 1 << 0 ) ) != 0:
            # Bit 0 is set! PANIC!
            return AstelcoTelescopeStatus.PANIC
        elif (status & ( 1 << 1 ) ) != 0:
            return AstelcoTelescopeStatus.ERROR
        elif (status & ( 1 << 2 ) ) != 0:
            return AstelcoTelescopeStatus.WARNING
        elif (status & ( 1 << 3 ) ) != 0:
            return AstelcoTelescopeStatus.INFO

        return AstelcoTelescopeStatus.OK

    def logStatus(self):
        tpl = self.getTPL()

        list = tpl.getobject('TELESCOPE.STATUS.LIST')
        block = list.split(',')
        # TODO: Improve separation of information for logging
        for group in block:
            self.log.debug(group)

    def acknowledgeEvents(self):
        '''
        Try to resolve any issue with the telescope by acknowledging its existence. This may
        resolve most of the common issues. Depending on the severity, the telescope may be
        in an error state even after acknowledging.

        :return: True  - acknowledge registered
                 False - acknowledge ignored
        '''

        # Get GLOBAL STATUS
        tpl = self.getTPL()

        status = tpl.getobject('TELESCOPE.STATUS.GLOBAL')
        if status > 0:
            self.log.debug("Telescope status not OK... Trying to acknowledge...")
            # writing GLOBAL status to CLEAR is how you acknowledge
            cmdid = tpl.set('TELESCOPE.STATUS.CLEAR', status)
            # if clear gets new value, acknowledge may have worked
            # self.waitCmd(cmdid, time.time(), self["maxidletime"])
            # clear = self._tpl.getobject('TELESCOPE.STATUS.CLEAR')
            # if clear == status:
            #    self.log.debug("CLEAR accepted new value...")
            # I will go ahead and check status anyway...
            # if GLOBAL is zero, than acknowledge worked
            oldstatus = status
            status = tpl.getobject('TELESCOPE.STATUS.GLOBAL')

            self.log.debug('Telescope status: %s | Global status: %s'%(oldstatus,status))
            if status == 0:
                self.log.debug('Acknowledge accepted...')
                return True
            else:
                self.log.warning('Acknowledge refused...')
                return False
        else:
            self.log.debug('Telescope status OK...')
            return True

    @lock
    def unpark(self):  # converted to Astelco

        if not self.isParked():
            return True

        tpl = self.getTPL()

        # Checking Telescope state
        state = tpl.getobject('TELESCOPE.READY_STATE')
        if state == -3:
            AstelcoException('Telescope in local mode. Check cabinet.')
        elif state == -2:
            AstelcoException('Emergency stop.')
        elif state == -1:
            AstelcoException('Error block telescope operation.')
        elif 0. < state < 1.:
            self.log.critical('Telescope already powering up.')
            return False

        cmdid = tpl.set('TELESCOPE.READY', 1, wait=False)

        # 2. start tracking
        #self.startTracking()
        ready_state = 0.0
        start_time = time.time()
        self._abort.clear()

        while ready_state < 1.0:
            self.log.debug("Powering up Astelco: %s" % (ready_state))
            old_ready_state = ready_state
            ready_state = tpl.getobject('TELESCOPE.READY_STATE')

            if ready_state != old_ready_state:
                self.log.debug("Powering up Astelco: %s" % (ready_state))
                old_ready_state = ready_state
            if self._abort.set():
                # Send abort command to astelco
                self.log.warning("Aborting! This will leave the telescope in an intermediate state!")
                tpl.set('ABORT', cmdid)
                return False
            if time.time() > start_time + self['parktimeout']:
                self.log.error("Parking operation timedout!")
                tpl.set('ABORT', cmdid)
                raise AstelcoException('Unparking telescope timedout.')

            status = self.getTelescopeStatus()
            if status == AstelcoTelescopeStatus.WARNING or status == AstelcoTelescopeStatus.INFO:
                self.log.warning("Acknowledging telescope state.")
                self.logStatus()
                self.acknowledgeEvents() # This is needed so I can tell the telescope to park
            elif status == AstelcoTelescopeStatus.ERROR or status == AstelcoTelescopeStatus.PANIC:
                # When something really bad happens during unpark, telescope needs to be parked
                # and then, start over.
                tpl.set('ABORT', cmdid)
                self.log.critical("Something wrong with the telescope. Aborting...")
                self.logStatus()
                # self.acknowledgeEvents() # This is needed so I can tell the telescope to park afterwards
                errmsg = '''Something wrong happened while trying to unpark the telescope. In most cases this happens
                when one of the submodules (like the hexapod) is not properly loaded or working pressure could not be
                reached. Waiting a couple of minutes, parking and unparking it again should solve the problem or sending
                someone there to check on the compressor. If that doesn't work, there may be a more serious problem with
                the system.'''
                raise AstelcoException(errmsg)

            time.sleep(.1)

        # 3. set location, date and time
        self._initTelescope()

        # 4. sync on park position (not really necessary when parking
        # on DEC=0, RA=LST

        # convert from park position to RA/DEC using the last LST set on 2.
        #ra = 0
        #dec = 0

        #if not self.sync (ra, dec):
        #    return False

        self.unparkComplete()
        self._parked = False
        return tpl.succeeded(cmdid)

    @lock
    def openCover(self):
        if self.isOpen():
            return True
        tpl = self.getTPL()
        cmdid = tpl.set('AUXILIARY.COVER.TARGETPOS', 1, wait=True)

        self.log.debug('Opening telescope cover...')

        ready_state = 0.0
        while ready_state < 1.0:
            self.log.debug("Opening telescope cover: %s" % (ready_state))
            #old_ready_state = ready_state
            ready_state = tpl.getobject('AUXILIARY.COVER.REALPOS')
            #if ready_state != old_ready_state:
            #    self.log.debug("Powering up Astelco: %s"%(ready_state))
            #    old_ready_state = ready_state
            time.sleep(5.0)

        return tpl.succeeded(cmdid)

    @lock
    def closeCover(self):
        if not self.isOpen():
            return True

        self.log.debug('Closing telescope cover...')
        tpl = self.getTPL()
        cmdid = tpl.set('AUXILIARY.COVER.TARGETPOS', 0, wait=True)

        ready_state = 1.0
        while ready_state > 0.0:
            self.log.debug("Closing telescope cover: %s" % (ready_state))
            #old_ready_state = ready_state
            ready_state = tpl.getobject('AUXILIARY.COVER.REALPOS')
            #if ready_state != old_ready_state:
            #    self.log.debug("Powering up Astelco: %s"%(ready_state))
            #    old_ready_state = ready_state
            time.sleep(5.0)

        return True  #self._tpl.succeeded(cmdid)

    # low-level
    def _debug(self, msg):  # no need to convert to Astelco
        if self._debugLog:
            print >> self._debugLog, time.time(), threading.currentThread().getName(), msg
            self._debugLog.flush()

    def _read(self, n=1, flush=True):  # not used for Astelco
        if not self._tty.isOpen():
            raise IOError("Device not open")

        if flush:
            self._tty.flushInput()

        ret = self._tty.read(n)
        self._debug("[read ] %s" % repr(ret))
        return ret

    def _readline(self, eol='#'):  # not used for Astelco
        if not self._tty.isOpen():
            raise IOError("Device not open")

        ret = self._tty.readline(None, eol)
        self._debug("[read ] %s" % repr(ret))
        return ret

    def _readbool(self):  # not used for Astelco
        try:
            ret = int(self._read(1))
        except ValueError:
            return False

        if not ret:
            return False

        return True

    def _write(self, data, flush=True):  # not used for Astelco
        if not self._tty.isOpen():
            raise IOError("Device not open")

        if flush:
            self._tty.flushOutput()

        self._debug("[write] %s" % repr(data))

        return self._tty.write(data)

    def getobject(self, object):
        tpl = self.getTPL()
        return tpl.getobject(object)

    def set(self, object, value, wait=False, binary=False):
        tpl = self.getTPL()
        return tpl.set(object, value, wait=False, binary=False)

    def getcommands_sent(self):
        tpl = self.getTPL()
        return tpl.commands_sent

    def getSensors(self):
        return self.sensors

    @lock
    def updateSensors(self):

        sensors = [('SENSTIME','%s'%dt.datetime.now(),"Last time sensors where updated.")]

        tpl = self.getTPL()

        for n in range(int(self["sensors"])):
            description = tpl.getobject('AUXILIARY.SENSOR[%i].DESCRIPTION' % (n + 1))

            if not description:
                continue
            elif "FAILED" in description:
                continue

            value = tpl.getobject('AUXILIARY.SENSOR[%i].VALUE' % (n + 1))
            unit = tpl.getobject('AUXILIARY.SENSOR[%i].UNITY' % (n + 1))
            sensors.append((description, value, unit))
            # sensors.append((0, 0, 0))

        self.sensors = sensors

    def getMetadata(self, request):
        lst = self.getLocalSiderealTime()
        baseHDR = [('TELESCOP', self['model'], 'Telescope Model'),
                ('OPTICS', self['optics'], 'Telescope Optics Type'),
                ('MOUNT', self['mount'], 'Telescope Mount Type'),
                ('APERTURE', self['aperture'], 'Telescope aperture size [mm]'),
                ('F_LENGTH', self['focal_length'],
                 'Telescope focal length [mm]'),
                ('F_REDUCT', self['focal_reduction'],
                 'Telescope focal reduction'),
                ('RA', self.getRa().toHMS().__str__(),
                 'Right ascension of the observed object'),
                ('DEC', self.getDec().toDMS().__str__(),
                 'Declination of the observed object'),
                ("EQUINOX", 2000.0, "coordinate epoch"),
                ('ALT', self.getAlt().toDMS().__str__(),
                 'Altitude of the observed object'),
                ('AZ', self.getAz().toDMS().__str__(),
                 'Azimuth of the observed object'),
                ("WCSAXES", 2, "wcs dimensionality"),
                ("RADESYS", "ICRS", "frame of reference"),
                ("CRVAL1", self.getRa().D,
                 "coordinate system value at reference pixel"),
                ("CRVAL2", self.getDec().D,
                 "coordinate system value at reference pixel"),
                ("CTYPE1", 'RA---TAN', "name of the coordinate axis"),
                ("CTYPE2", 'DEC--TAN', "name of the coordinate axis"),
                ("CUNIT1", 'deg', "units of coordinate value"),
                ("CUNIT2", 'deg', "units of coordinate value")] + self.getSensors()

        ra = None
        for i in range(len(baseHDR)):
            if baseHDR[i][0] == "RA":
                ra = Coord.fromHMS(baseHDR[i][1])
        if ra is None:
            ra = self.getRa()
        HA = lst - ra
        RAoffset = Coord.fromD(self._getOffset(Direction.E))
        DECoffset = Coord.fromD(self._getOffset(Direction.N))

        newHDR = [('RAOFFSET',RAoffset.toDMS().__str__(),"Current offset of the telescope in RA (DD:MM:SS.SS)."),
                  ('DEOFFSET',DECoffset.toDMS().__str__(),"Current offset of the telescope in Declination (DD:MM:SS.SS)."),
                  ('TEL_LST',lst.toHMS().__str__(),"Local Sidereal Time at the start of the observation (HH:MM:SS.SS)."),
                  ('TEL_HA',HA.toHMS().__str__(),"Hour Angle at the start of the observation (HH:MM:SS.SS).")]

        for new in newHDR:
            baseHDR.append(new)

        return baseHDR

    #     return [('TELESCOP', self['model'], 'Telescope Model'),
    #             ('OPTICS', self['optics'], 'Telescope Optics Type'),
    #             ('MOUNT', self['mount'], 'Telescope Mount Type'),
    #             ('APERTURE', self['aperture'], 'Telescope aperture size [mm]'),
    #             ('F_LENGTH', self['focal_length'],
    #              'Telescope focal length [mm]'),
    #             ('F_REDUCT', self['focal_reduction'],
    #              'Telescope focal reduction'),
    #             # TODO: Convert coordinates to proper equinox
    #             # TODO: How to get ra,dec at start of exposure (not end)
    #             ('RA', self._getRa().toHMS().__str__(),
    #              'Right ascension of the observed object'),
    #             ('DEC', self._getDec().toDMS().__str__(),
    #              'Declination of the observed object'),
    #             ("EQUINOX", 2000.0, "coordinate epoch"),
    #             ('ALT', self._getAlt().toDMS().__str__(),
    #              'Altitude of the observed object'),
    #             ('AZ', self._getAz().toDMS().__str__(),
    #              'Azimuth of the observed object'),
    #             ("WCSAXES", 2, "wcs dimensionality"),
    #             ("RADESYS", "ICRS", "frame of reference"),
    #             ("CRVAL1", self._getRa().D,
    #              "coordinate system value at reference pixel"),
    #             ("CRVAL2", self._getDec().D,
    #              "coordinate system value at reference pixel"),
    #             ("CTYPE1", 'RA---TAN', "name of the coordinate axis"),
    #             ("CTYPE2", 'DEC--TAN', "name of the coordinate axis"),
    #             ("CUNIT1", 'deg', "units of coordinate value"),
    #             ("CUNIT2", 'deg', "units of coordinate value")] + self.getSensors()
