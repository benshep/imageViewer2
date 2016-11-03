# -*- coding: utf-8 -*-
# encoding=utf8  
"""
Python frame grabber application for ALICE 

v2.6 (3/4/16): tidied up code
v2.5 (18/2/16): save sequences of images as PNGs or movies
v2.4 (15/2/16): toggle button for screen in/out
    fixed frequent crashes due to thread clashes
    new: click image to play/pause
    show FWHM, not sigma
    better calculation of frame rate
    lots of options
v2.3 (11/2/16): implemented LMFIT for image processing - now grabs at 25 Hz!
v2.2 (4/2/16): added grids
v2.1 (28/1/16): added option to auto-move screens (or not)
v2.0: converted MATLAB application to Python

Created on Thu May 14 20:46:59 2015

@author: Ben Shepherd

Image credits:
Play.png: http://www.flaticon.com/free-icon/music-player-play_70409
Pause.png: http://www.flaticon.com/free-icon/music-player-pause-lines_70419
LEDs_On.png, LEDs_Off.png: http://www.flaticon.com/free-icon/car-beacon-on_20922
eye.png: http://www.flaticon.com/free-icon/eye-outline_23912
hourglass.png, hourglass_256.png: freepik, http://www.flaticon.com/free-icon/hourglass_65560
question.png: http://www.flaticon.com/free-icon/question-mark_3711#term=help&page=1&position=1
pile-of-paper.png: http://www.flaticon.com/free-icon/pile-of-paper_843?#term=stack&page=2&position=42
movie.png: http://www.flaticon.com/free-icon/movie-film_62686?#term=movie&page=1&position=3
hamburger-menu.png: http://www.flaticon.com/free-icon/menu-button-of-three-horizontal-lines_56763
tools.png: http://www.flaticon.com/free-icon/screwdriver-and-wrench-crossed_9156

NOTE: to install new packages
python.exe -m pip install openpyxl
(for instance)
"""

from PyQt4 import QtCore, QtGui #for GUI building
from pyqtgraph import PlotWidget #plotting profiles and fits
import ctypes #communication with frame grabber library
import sys
import os
from datetime import datetime, timedelta
import time
import numpy as np
from serial import Serial #communication with video switchers
from epics import PV #communication with control system
from threading import Thread, Lock #concurrent image analysis
from functools import partial #adding parameters to callback functions
from openpyxl import load_workbook #for calibration data
import zmq #for messaging
import webbrowser #to get help
from lmfit.models import GaussianModel #for fitting
import subprocess #for saving movies using ffmpeg

# debug on PCs other than ALICE console
# in debug mode, a sequence of saved images will be shown instead
DEBUG = not os.getenv('COMPUTERNAME').lower() == 'alicedell6'
if DEBUG:
    import glob
    from scipy.misc import imread

def CheckSuccess(result, func, arguments):
    "Function to check for valid return codes in calls to frame grabber library."
    if result == 0:
        raise Exception("function '%s' failed" % func.__name__)
    else:
        return result
        
class HGRABBER_t(ctypes.Structure):
    "Struct to hold the handle to a grabber object"
    _fields_ = [("unused", ctypes.c_int)]


def ledsOnOff(leds, on):
    [ledPV[on].put(1) for ledPV in leds.values()]

class Screen():
    """Screen class. Encapsulates information about how to move each screen in and out,
       where it is in the multiplexer, thresholds for finding an image,
       and calibration from pixels to mm."""
    def __init__(self, name, muxID, threshold, stdThreshold, pvName='', screenIn='', screenOut=''):
        self.name = name
        self.muxID = muxID
        self.threshold = threshold
        self.stdThreshold = stdThreshold
        self.overlayFiles = []
        self.gridImage = None
        # PV name should be REG-DIA-YAG-01:On
        try:
            region, num = name.split('-')
        except ValueError: # name doesn't have a hyphen
            region = name
            num = ''
        screenType = 'YAG' if region == 'INJ' else 'OTR'
        
        if name[2:] == 'WGE':
            baseName = 'ST3-WIG-' + name + '-01'
        else:
            baseName = '-'.join([region, 'DIA', screenType, num.zfill(2)])
            
        self.pvPos = (PV(baseName + ':X'), PV(baseName + ':Y'))
        self.pvSize = (PV(baseName + ':W'), PV(baseName + ':H'))
        # readback should be <= statusIn to be considered IN
        # readback should be >= statusOut to be considered OUT
        # if there are any that go the other way, we need to add another flag
        if name == 'INJ-1':
            self.pvIn = PV(baseName + ':MSABS')
            self.setIn = -59.5
            self.pvOut = self.pvIn
            self.setOut = -105
            self.pvStatus = PV(baseName + ':RCAL')
            self.statusIn = -60
            self.statusOut = -95
        elif name == 'ST3-1':
            baseName = 'ST3-MOV-OTR02-01'
            self.pvIn = PV(baseName + ':MABS')
            self.setIn = 11600
            self.pvOut = self.pvIn
            self.setOut = -2300
            self.pvStatus = self.pvIn #TODO: is there a readback as well?
            self.statusIn = self.setIn
            self.statusOut = self.setOut
        else:
            self.pvIn = PV(baseName + ':On')
            self.setIn = 1
            self.pvOut = PV(baseName + ':Off')
            self.setOut = 1
            self.pvStatus = PV(baseName + ':Sta')
            self.statusIn = 1
            self.statusOut = 0
    
    def moveIn(self):
        self.pvIn.put(self.setIn)
    def moveOut(self):
        self.pvOut.put(self.setOut)

    def setPositionPVs(self, ax, pos, size):
        "Send the beam position to EPICS"
        self.pvPos[ax].put(pos)
        self.pvSize[ax].put(size)
        

def loadGridImage(screen, fileName):
    screen.gridImage = QtGui.QImage(fileName)
    
def setIcon(ctrl, fileName):
    "Sets the icon for the given control to a QIcon of the given PNG file."
    ctrl.setIcon(QtGui.QIcon(QtGui.QPixmap('Icons\\' + fileName + '.png')))
 
hand = QtCore.Qt.PointingHandCursor

class Window(QtGui.QWidget):
    # Signals to update items in the GUI (we can't do this outside the GUI thread)
    listItemIconChanged = QtCore.pyqtSignal(str, str)
    ledIconChanged = QtCore.pyqtSignal(bool)
    plotsChanged = QtCore.pyqtSignal(np.ndarray, np.ndarray)
    fitChanged = QtCore.pyqtSignal(int, np.ndarray, str)
    imageChanged = QtCore.pyqtSignal()
    captionChanged = QtCore.pyqtSignal(str)
    inOutLabelsChanged = QtCore.pyqtSignal(str, str)
    imageLabelChanged = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
#        t = datetime.now()
        QtGui.QWidget.__init__(self, parent)

        self.baseFolder = '\\\\fed.cclrc.ac.uk\\Org\\NLab\\ASTeC\\Projects\\ALICE'
        self.config = QtCore.QSettings('imageViewer.ini', QtCore.QSettings.IniFormat)

        if not DEBUG:
            # Initialise frame grabber
            libFileName = 'tisgrabber_x64.dll'
            self.grablib = ctypes.cdll.LoadLibrary(libFileName)
            HGRABBER = ctypes.POINTER(HGRABBER_t)
            # set up the argument and return types we will need: default return type is int
            restypeDict = {'CreateGrabber': HGRABBER, 'GetDevice': ctypes.c_char_p, 
                           'GetImagePtr': ctypes.POINTER(ctypes.c_char), 
                           'ReleaseGrabber': None, 'CloseLibrary': None,
                           'CloseVideoCaptureDevice': None, 'StopLive': None}
            for fcnName, restype in restypeDict.items():
                getattr(self.grablib, 'IC_' + fcnName).restype = restype
          
            # attach error checks to the functions we expect to return non-zero
            fcnList = ['InitLibrary', 'GetDeviceCount', 'CreateGrabber',
                       'OpenVideoCaptureDevice', 'GetVideoFormatWidth',
                       'GetVideoFormatHeight', 'PrepareLive', 'SetFormat',
                       'StartLive', 'SnapImage', 'GetImagePtr', 'SetFrameReadyCallback',
                       'SetContinuousMode']
            for fcnName in fcnList:
                getattr(self.grablib, 'IC_' + fcnName).errcheck = CheckSuccess
            
            self.grablib.IC_InitLibrary()
            self.grablib.IC_GetDeviceCount() #this will raise exception if zero
            self.hGrabber = self.grablib.IC_CreateGrabber()
            
            expectedDeviceName = b'DFG/USB2pro'
            deviceName = self.grablib.IC_GetDevice(0)
            if not deviceName == expectedDeviceName:
                raise Exception("device name is '{:s}', expecting '{:s}'".format(
                    deviceName.decode(), expectedDeviceName.decode()))
            
            self.grablib.IC_OpenVideoCaptureDevice(self.hGrabber, deviceName)
            
            self.imgWidth = self.grablib.IC_GetVideoFormatWidth(self.hGrabber)
            self.imgHeight = self.grablib.IC_GetVideoFormatHeight(self.hGrabber) - 4
#            print('Initialised frame grabber', (datetime.now() - t).total_seconds())
        else:
            self.imgWidth = 768
            self.imgHeight = 572
            
        self.nBytes = self.imgWidth * self.imgHeight

        # ignore the last 4 columns (they're usually close to zero)
        self.xr = range(self.imgWidth - 4)
        # interlacing means we only need half of the frame
        self.yr = range(self.imgHeight // 2)       
        
        if not DEBUG:
            startup_grabber_thread = Thread(target=self.startupGrabber)
            startup_grabber_thread.start()
#            print('StartLive', (datetime.now() - t).total_seconds())
        else:
            folder = r'\\fed.cclrc.ac.uk\Org\NLab\ASTeC\Projects\ALICE\Work\2016\01\31'
            
            files = glob.glob(folder + r'\*.png')
            self.testImages = np.empty([len(files), self.imgHeight, self.imgWidth], dtype='uint8')
            for i, imFileName in enumerate(files):
                self.testImages[i] = imread(imFileName) #returns a numpy array
        
        # For messaging
        self.context = zmq.Context()
        self.dataPublisher = self.context.socket(zmq.PUB)
        self.dataPublisher.bind("tcp://*:5556")
        self.serverThread = Thread(target=self.cameraChangeServer)
        self.serverThread.start()
#        print('Started server', (datetime.now() - t).total_seconds())
        
        # Create GUI        
        self.layout = QtGui.QGridLayout()
        
        self.grabbing = True
        
        self.btnLEDs = QtGui.QPushButton('LEDs')
        self.btnLEDs.setMaximumWidth(100)
        self.applyButtonProperties(self.btnLEDs, 'LEDs_Off', 
                                   self.btnLEDs_clicked, 1, 0, toggle=True)
        # LED names: XXX-DIA-SCLED-01:(On|Off|Sta)
        leds = ['INJ', 'ST1', 'ST2', 'ST3']
        pvName = '-DIA-SCLED-01:'
        self.pvLEDs = {}
        for led in leds:
            self.pvLEDs[led] = {}
            self.pvLEDs[led][False] = PV(led + pvName + 'Off')
            self.pvLEDs[led][True] = PV(led + pvName + 'On')
            self.pvLEDs[led]['status'] = PV(led + pvName + 'Sta', callback=self.ledStatusChanged, connection_timeout=10)
            self.pvLEDs[led]['status'].really_connected = False

        # hacky toggle button made from a slider, a bit clearer than an in/out button
        self.sldMoveScreen = QtGui.QSlider(QtCore.Qt.Horizontal)
        self.sldMoveScreen.setMaximum(1)
        self.sldMoveScreen.setTracking(False)
        styleSheet = """
        QSlider::groove:horizontal {
            border: 1px solid #999999;
            height: 100%; /* the groove expands to the size of the slider by default. by giving it a height, it has a fixed size */
            background: #dddddd;
            margin: 2px 0;
        }
        QSlider::handle:horizontal {
            background: #111111;
            border: 1px solid #5c5c5c;
            height: 100%;
            width: 12;
            margin: -2px 0; /* handle is placed by default on the contents rect of the groove. Expand outside the groove */
            border-radius: 3px;
        }"""
        self.sldMoveScreen.setStyleSheet(styleSheet)
        self.lblScreenOut = QtGui.QLabel('OUT')
        self.lblScreenOut.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.lblScreenOut.setMaximumWidth(25)
        self.lblScreenOut.setCursor(hand)
        self.sldMoveScreen.state = 0
        self.sldMoveScreen.valueChanged.connect(self.btnMoveScreen_clicked)
        self.sldMoveScreen.setMaximumWidth(40)
        self.lblScreenIn = QtGui.QLabel('IN')
        self.lblScreenIn.setMaximumWidth(25)
        self.lblScreenIn.setCursor(hand)
        screenToggle = QtGui.QHBoxLayout()
        screenToggle.addWidget(self.lblScreenOut)
        screenToggle.addWidget(self.sldMoveScreen)
        screenToggle.addWidget(self.lblScreenIn)
        screenToggle.setContentsMargins(0, 0, 0, 0)
        self.inOutLabelsChanged.connect(self.updateInOutLabelColours)
        self.layout.addLayout(screenToggle, 0, 0)
        
        hbox = QtGui.QHBoxLayout()
        
        styleSheet = '''QToolButton::menu-indicator {width: 8px; height: 8px; top: -6px; left: -4px}
                        QToolButton {padding-right: 12px}'''
        self.btnOptions = QtGui.QToolButton()
        self.btnOptions.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        setIcon(self.btnOptions, 'hamburger-menu')
        self.btnOptions.setMinimumHeight(24)
        self.btnOptions.setText('Options')
        self.btnOptions.setPopupMode(QtGui.QToolButton.InstantPopup)
        self.btnOptions.setStyleSheet(styleSheet)
        menuOptions = QtGui.QMenu(self.btnOptions)

        opts = [('autoMove', 'Automatically move screens in/out'), 
                ('beamOnly', 'Show only frames with beam'),
                ('useStd', 'Use stdev (default is sum)'),
                ('fitGaussians', 'Fit Gaussians to profiles'),
                ('outputToEPICS', 'Output data to EPICS'),
                ('threading', 'Process images in background')]
        self.options = {}
        for opt, desc in opts:
            ctrl = menuOptions.addAction(desc)
            ctrl.setCheckable(True)
            chk = self.config.value(opt, 'true') == 'true'
            ctrl.setChecked(chk)
            self.options[opt] = ctrl

        self.options['useStd'].triggered.connect(self.useStd_changed)

        menuThreshold = menuOptions.addMenu('Beam detection threshold')
        threshold = QtGui.QWidgetAction(self.btnOptions)
        self.thresSpinBox = QtGui.QDoubleSpinBox()
        self.thresSpinBox.setMinimum(0)
        self.thresSpinBox.setSingleStep(0.002)
        self.thresSpinBox.setDecimals(3)
        self.thresSpinBox.valueChanged.connect(self.thresholdSpin_changed)
        threshold.setDefaultWidget(self.thresSpinBox)
        menuThreshold.addAction(threshold)
        self.thresholdReset = menuThreshold.addAction('Reset')
        self.thresholdReset.setEnabled(False)
        self.thresholdReset.triggered.connect(self.thresholdReset_clicked)
        
        menuDeinterlacing = menuOptions.addMenu('Image deinterlacing')
        di_group = QtGui.QActionGroup(self.btnOptions)
        opts = [('noDeinterlacing', 'None'),
                ('deinterlace', 'Show brightest field'),
                ('subtractDim', 'Subtract dim field')]
        for opt, desc in opts:
            ctrl = menuDeinterlacing.addAction(desc)
            ctrl.setCheckable(True)
            chk = self.config.value(opt, 'true' if opt == 'deinterlace' else 'false') == 'true'
            ctrl.setChecked(chk)
            ctrl.setActionGroup(di_group)
            self.options[opt] = ctrl
            
        self.btnOptions.setMenu(menuOptions)
        hbox.addWidget(self.btnOptions)
        
        self.btnTools = QtGui.QToolButton()
        self.btnTools.setMinimumHeight(24)
        self.btnTools.setText('Tools')
        self.btnTools.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        setIcon(self.btnTools, 'tools')
        self.btnTools.setPopupMode(QtGui.QToolButton.InstantPopup)
        self.btnTools.setStyleSheet(styleSheet)
        menuTools = QtGui.QMenu(self.btnTools)

        saveSeq = QtGui.QAction('Save sequence of images...', self.btnTools)
        setIcon(saveSeq, 'pile-of-paper')
        saveSeq.triggered.connect(partial(self.startSequence, 'png'))
        self.seqI = -1 #not running sequence
        self.imgSequence = []
        menuTools.addAction(saveSeq)
        self.ffmpegPath = r'C:\Program Files\ffmpeg\bin\ffmpeg.exe'

        saveMovie = QtGui.QAction('Record movie...', self.btnTools)
        setIcon(saveMovie, 'movie')
        saveMovie.triggered.connect(partial(self.startSequence, 'movie'))
        saveMovie.setEnabled(os.path.isfile(self.ffmpegPath))
        menuTools.addAction(saveMovie)
        self.btnTools.setMenu(menuTools)
        hbox.addWidget(self.btnTools)
        
        self.screen_in = False
                                   
        self.btnMinTL = QtGui.QToolButton()
        self.applyButtonProperties(self.btnMinTL, 'brightness-off', 
                                   self.btnMinTL_clicked, 1, 2, toggle=True,
                                   tip='Set minimum train length (24 ns)')
                                   
        self.btnReduceTL = QtGui.QToolButton()
        self.applyButtonProperties(self.btnReduceTL, 'brightness-low', self.btnReduceTL_clicked, 1, 3, tip='Train length ÷ 2')
                                   
        self.lblTrainLength = QtGui.QLabel('')
        self.layout.addWidget(self.lblTrainLength, 1, 4, 1, 1)
        self.pvTrainLengthSet = PV('INJ-LSR-DLY-01:BCSET')
        self.pvTrainLengthRead = PV('INJ-LSR-DLY-01:BCAL', callback=self.trainLengthChanged)
        self.trainLengthStops = np.array([0.024, 0.048, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100])
        sizePolicy = QtGui.QSizePolicy(QtGui.QSizePolicy.Maximum, QtGui.QSizePolicy.Ignored)
        self.lblTrainLength.setSizePolicy(sizePolicy)
        self.lblTrainLength.setMinimumWidth(40)
        self.lblTrainLength.setAlignment(QtCore.Qt.AlignCenter | QtCore.Qt.AlignVCenter)
        self.btnIncreaseTL = QtGui.QToolButton()
        self.applyButtonProperties(self.btnIncreaseTL, 'brightness-high', self.btnIncreaseTL_clicked, 1, 5, tip='Train length x 2')
                                   
        self.chkGrid = QtGui.QCheckBox('Grid')
        self.layout.addWidget(self.chkGrid, 1, 6, 1, 1)
        self.chkGrid.clicked.connect(self.chkGrid_clicked)
        
        self.chkOverlay = QtGui.QCheckBox('Overlay')
        self.layout.addWidget(self.chkOverlay, 1, 7, 1, 1)
        self.chkOverlay.clicked.connect(self.chkOverlay_clicked)
        
        self.cbSetup = QtGui.QComboBox()
        self.cbSetup.activated.connect(self.cbSetup_clicked)
        self.layout.addWidget(self.cbSetup, 1, 8, 1, 1)
        self.btnPrevOverlay = QtGui.QToolButton()
        self.applyButtonProperties(self.btnPrevOverlay, 'left', self.btnPrevOverlay_clicked, 1, 9)
        self.btnPrevOverlay.setEnabled(False)
        self.btnNextOverlay = QtGui.QToolButton()
        self.applyButtonProperties(self.btnNextOverlay, 'right', self.btnNextOverlay_clicked, 1, 10)
        self.btnNextOverlay.setEnabled(False)
        self.lblOverlayFileName = QtGui.QLabel('')
        fixed = QtGui.QSizePolicy.Fixed
        sizePolicy = QtGui.QSizePolicy(QtGui.QSizePolicy.Ignored, fixed)
        self.lblOverlayFileName.setSizePolicy(sizePolicy)
        self.lblOverlayFileName.setVisible(False)
#        self.lblOverlayFileName.setOpenExternalLinks(True)
        self.lblOverlayFileName.linkActivated.connect(self.lblOverlayFileName_linkClicked)
        self.imageLabelChanged.connect(self.updateImageLabel)
#        self.lblOverlayFileName.linkHovered.connect(self.lblOverlayFileName_linkClicked)
        self.layout.addWidget(self.lblOverlayFileName, 1, 11, 1, 3)
                                   
        # Open serial ports for communication with video switchers
        if not DEBUG:
            self.videoSwitchers = [Serial(p) for p in [3, 4, 5]] #COM4,5,6

        self.lvCameras = QtGui.QListWidget()
        # Make sure icons are shown on the right (for neatness!)
        self.lvCameras.setLayoutDirection(QtCore.Qt.RightToLeft)
        self.listItemIconChanged.connect(self.updateListItemIcon)
        self.ledIconChanged.connect(self.updateLEDIcon)
        self.imageChanged.connect(self.updateImage)
        self.plotsChanged.connect(self.updatePlots)
        self.fitChanged.connect(self.updateFit)
        self.captionChanged.connect(self.updateCaption)
        # each tuple here: name, muxID
        camList = [('INJ-1', 1), ('INJ-2', 2), ('INJ-3', 3), 
                   ('INJ-4', 4), ('INJ-5', 5), ('ST1-1', 6), 
                   ('ST1-2', 7), ('ST1-3', 8), ('ST1-4', 28), 
                   ('AR1-1', 11), ('AR1-2', 12), ('ST2-1', 13), 
                   ('ST2-2', 14), ('ST2-Y1', 23), ('ST2-3', 15), 
                   ('UPWGE', 24), ('CNWGE', 25), ('DNWGE', 26), 
                   ('ST3-1', 16), ('AR2-1', 17), ('AR2-2', 18),
                   ('ST4-1', 19), ('ST4-2', 20)]
        self.cameras = {}
        itemHeight = self.imgHeight // len(camList) - 1
        self.selectedCamera = self.config.value('selectedScreen', camList[0][0])

        gridImgFolder = self.baseFolder + '\\Analysis\\YAG Images Calibration\\Grids\\'
#        print('Before set up cameras', (datetime.now() - t).total_seconds())
        for name, muxID in camList:
#            tt = datetime.now()
            item = QtGui.QListWidgetItem(name)
            #align text left even though overall widget layout is right-to-left
            item.setTextAlignment(QtCore.Qt.AlignAbsolute | QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            self.lvCameras.addItem(item)
            if self.selectedCamera == name:
                self.lvCameras.setCurrentItem(item)
            threshold = self.config.value(name + '-threshold', 0.002)
            stdThreshold = self.config.value(name + '-stdev-threshold', 0.002)
            screen = Screen(name, muxID, threshold, stdThreshold)
#            print(name, (datetime.now() - tt).total_seconds())
            screen.listItem = item
            fileName = gridImgFolder + name + '.png' # should fail silently if file doesn't exist
            Thread(target=loadGridImage, args=(screen, fileName)).start() #do it in background
#            screen.gridImage = QtGui.QPixmap(fileName)
            item.setSizeHint(QtCore.QSize(10, itemHeight))
            self.cameras[name] = screen
            self.cameras[name].pvStatus.add_callback(partial(self.screenStatusChanged, name))
#        self.lvCameras.setCurrentRow(initCamera)
        self.lvCameras.currentItemChanged.connect(self.lvCameras_itemChanged)
        self.lvCameras.itemDoubleClicked.connect(self.lvCameras_itemDoubleClicked)
        self.lvCameras.setMinimumWidth(self.lvCameras.sizeHintForColumn(0))
        self.lvCameras.setMaximumWidth(100)
        self.layout.addWidget(self.lvCameras, 2, 0, 1, 1)
#        print('Set up cameras', (datetime.now() - t).total_seconds())
        
        # Get calibration data
        calDataFileName = self.baseFolder + '\\Analysis\\YAG Images Calibration\\Image calibration.xlsx'
        load_caldata_thread = Thread(target=self.loadCalibrationData, args=(calDataFileName,))
        load_caldata_thread.start()
        
        self.lblImage = QtGui.QLabel()
        sizePolicy = QtGui.QSizePolicy(fixed, fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.lblImage.sizePolicy().hasHeightForWidth())
        self.lblImage.setSizePolicy(sizePolicy)
        self.imgQSize = QtCore.QSize(self.imgWidth, self.imgHeight)
        self.lblImage.setMinimumSize(self.imgQSize)
#        self.lblImage.setOrientation(QtCore.Qt.Vertical)
        colspan = 10 # number of widgets spanning width of image display
        self.lblImage.setCursor(hand)
        self.layout.addWidget(self.lblImage, 2, 1, 1, colspan)

        # overlay image
        self.lblOverlay = QtGui.QLabel()
#        self.lblOverlay.setSizePolicy(sizePolicy)
        self.lblOverlay.setMinimumSize(self.imgQSize)
        self.lblOverlay.setCursor(hand)
        self.layout.addWidget(self.lblOverlay, 2, 1, 1, colspan)
        opacity = QtGui.QGraphicsOpacityEffect()
        opacity.setOpacity(0.5)
        self.lblOverlay.setGraphicsEffect(opacity)
        self.lblOverlay.setVisible(False)

        # grid image
        self.lblGrid = QtGui.QLabel()
#        self.lblOverlay.setSizePolicy(sizePolicy)
        self.lblGrid.setMinimumSize(self.imgQSize)
        self.lblGrid.setCursor(hand)
        self.layout.addWidget(self.lblGrid, 2, 1, 1, colspan)
#        self.lblGrid.setGraphicsEffect(opacity)
        self.lblGrid.setVisible(False)

        # play/pause control
        self.lblPlayPause = QtGui.QLabel()
        self.lblPlayPause.setPixmap(QtGui.QPixmap('Icons\\Pause.png'))
        self.lblPlayPause.setAlignment(QtCore.Qt.AlignHCenter | QtCore.Qt.AlignVCenter)
        self.lblPlayPause.setCursor(hand)
        self.layout.addWidget(self.lblPlayPause, 2, 1, 1, colspan)
        self.lblPlayPause.setVisible(False)
        
        self.txtFileName = QtGui.QLineEdit()
        hbox.addWidget(self.txtFileName)
        self.btnReset_clicked()
        self.btnReset = QtGui.QPushButton('Reset')
        self.applyButtonProperties(self.btnReset, 'refresh', self.btnReset_clicked, 0, colspan + 1, parent=hbox)
        self.btnSave = QtGui.QPushButton('Save')
        self.applyButtonProperties(self.btnSave, 'download', self.btnSave_clicked, 0, colspan + 2, parent=hbox)
        self.btnHelp = QtGui.QPushButton('Help')
        self.applyButtonProperties(self.btnHelp, 'question', self.btnHelp_clicked, 0, colspan + 3, parent=hbox)
        self.layout.addLayout(hbox, 0, 2, 1, 12)
        #addWidget syntax: (widget, row, col, rowspan, colspan)

        self.lblCaption = QtGui.QLabel('')
        self.layout.addWidget(self.lblCaption, 0, 1, 2, 1)
        
        rowspan = 3 # large controls along the bottom take up this many rows

        # X profile (underneath image)
        xProfilePlot = PlotWidget()
#        xProfilePlot.plotItem.hideAxis('bottom')
        xProfilePlot.plotItem.hideAxis('left')
        self.layout.addWidget(xProfilePlot, 3, 1, rowspan, colspan)

        # Y profile 
        yProfilePlot = PlotWidget()
#        yProfilePlot.plotItem.hideAxis('left')
        yProfilePlot.plotItem.hideAxis('bottom')
        self.layout.addWidget(yProfilePlot, 2, colspan + 1, 1, 3)
        self.profilePlot = [xProfilePlot, yProfilePlot]

        lblXData = QtGui.QLabel('')
        self.layout.addWidget(lblXData, 3, 1, rowspan, colspan)
        lblXData.setStyleSheet('color:orange; padding:20px')
        lblXData.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)

        lblYData = QtGui.QLabel('')
        self.layout.addWidget(lblYData, 2, colspan + 1, 1, 3)
        lblYData.setStyleSheet('color:orange; padding:20px')
        lblYData.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignTop)
        self.lblFitData = [lblXData, lblYData]
        
        self.lblSavedImage = QtGui.QLabel('')
        self.displayedImage = ''
        sizePolicy = QtGui.QSizePolicy(QtGui.QSizePolicy.Ignored, QtGui.QSizePolicy.Ignored)
        self.layout.addWidget(self.lblSavedImage, 3, colspan + 1, rowspan, 3)
        self.lblSavedImageCaption = QtGui.QLabel('')
        graphicsEffect = QtGui.QGraphicsDropShadowEffect()
        graphicsEffect.setColor(QtGui.QColor('black'))
        graphicsEffect.setOffset(QtCore.QPointF(2, 2))
        self.lblSavedImageCaption.setGraphicsEffect(graphicsEffect)
        self.lblSavedImageCaption.setSizePolicy(sizePolicy)
        self.lblSavedImage.wheelEvent = self.lblSavedImage_wheel
        self.lblSavedImage.mousePressEvent = self.lblSavedImage_clicked
        self.lblSavedImageCaption.setStyleSheet('color:white; padding:10px')
        self.lblSavedImageCaption.setAlignment(QtCore.Qt.AlignTop)
        self.lblSavedImageCaption.mousePressEvent = self.lblSavedImage_clicked
        self.lblSavedImageCaption.wheelEvent = self.lblSavedImage_wheel
        self.layout.addWidget(self.lblSavedImageCaption, 3, colspan + 1, rowspan, 3)

        self.setLayout(self.layout)
        try:
            self.resize(self.config.value('windowSize'))
            self.move(self.config.value('windowPosn'))
        except:
            pass
        self.setWindowTitle('imageViewer 2.5')
        self.setWindowIcon(QtGui.QIcon('camera3.ico'))
        
        self.imgTime = datetime.now()
        self.imgTimeList = []
        
        # 'jet' colour map used by MATLAB
        # http://uk.mathworks.com/help/matlab/ref/colormap.html
        self.jet = [QtGui.qRgb(0,0,b) for b in range(128, 256, 4)]
        self.jet.extend([QtGui.qRgb(0,g,255) for g in range(0, 256, 4)])
        self.jet.extend([QtGui.qRgb(r,255,255-r) for r in range(0, 256, 4)])
        self.jet.extend([QtGui.qRgb(255,255-g,0) for g in range(0, 256, 4)])
        self.jet.extend([QtGui.qRgb(255-r,0,0) for r in range(128, 256, 4)])
        for i in range(249, 256):
            self.jet[i] = QtGui.qRgb(255,255,255) #white for last 6 levels to show saturation (which starts at ~250)
        
        # Get all the setup folders from the server
        self.populateSetupList(self.config.value('overlaySetup', ''))
         
        # Update the file list for the selected setup
        self.updateOverlayFiles()        

        # Make sure a camera is selected in the list, and we send the
        # signal to the video switcher to change to that one
        self.lvCameras_itemChanged(self.lvCameras.currentItem(), None)

        # For profiling/testing - do a certain number of grabs, then quit
        # Set to -1 to continue forever
        self.grabCount = -1

        # Make sure we're ready with calibration and grabber startup
        load_caldata_thread.join()
        if not DEBUG:
            startup_grabber_thread.join() #make sure this has completed!

        # Run image grabbing process in separate thread
        self.threadLock = Lock() #from threading library
        self.grabThread = Thread(target=self.grabImage)
        self.grabThread.start()
#        print('Finished init', (datetime.now() - t).total_seconds())
    
    def startupGrabber(self):
        "Startup the frame grabber - runs in a separate thread for speedy startup."
        self.grablib.IC_PrepareLive(self.hGrabber, 0) #no window
        self.grablib.IC_SetFormat(self.hGrabber, 0) #Y800?
        self.grablib.IC_StartLive(self.hGrabber, 0) #no window
        # for some reason, we have to start and stop
        # otherwise the grabber will fail
        self.grablib.IC_StopLive(self.hGrabber)
        self.grablib.IC_StartLive(self.hGrabber, 0) #no window
    
    def loadCalibrationData(self, calDataFileName):
        "Load calibration data - runs in a separate thread for speedy startup."
        workbook = load_workbook(calDataFileName, data_only=True)        
        tableAddress = workbook.get_named_range('calTable').destinations
        sheet, address = tableAddress[0]
        calTable = sheet[address]
        row = calTable.send(None)
        header = [cell.value for cell in row]
        # ignore the last 4 columns (they're usually close to zero)
        xr = np.arange(self.imgWidth - 4)
        # interlacing means we only need half of the frame
        yr = np.arange(0, self.imgHeight, 2)
        
        # assume structure of header
        assert(header == ['screen', 'x multiplier', 'y multiplier', 'x centre', 'y centre'])
        for row in calTable:
            values = [cell.value for cell in row]
            try:
                cam = self.cameras[values[0]]
                xMult, yMult, xCentre, yCentre = values[1:]
                cam.xrd = np.array((xr - xCentre) * xMult)
                cam.yrd = np.array((yr - yCentre) * yMult)
                cam.units = 'mm'
            except KeyError:
                #ignore listed cameras that we don't know about
                cam.xrd = xr
                cam.yrd = yr
                cam.units = 'pixels'
    
    # Run a server (on a separate thread) that takes requests to change the camera
    def cameraChangeServer(self):
        self.shutdown = False
        socket = self.context.socket(zmq.REP)
        socket.setsockopt(zmq.RCVTIMEO, 1000) #timeout for receives after 1 second
        socket.bind('tcp://*:5559')
        while not self.shutdown:
            try:
                message = socket.recv_string()
                print('Received request: ' + message)
                if message in self.cameras.keys():
                    socket.send_string(message)
                    self.lvCameras.setCurrentItem(self.cameras[message].listItem)
                elif message == 'OUT':
                    socket.send_string(message)
                    self.screen_in = True
                    self.btnMoveScreen_clicked()
                elif message == 'IN':
                    socket.send_string(message)
                    self.screen_in = False
                    self.btnMoveScreen_clicked()
                elif message == 'LIVE':
                    socket.send_string(message)
                    self.grabbing = True
                elif message == 'PAUSE':
                    socket.send_string(message)
                    self.grabbing = False
                elif message == 'HELLO': #just a friendly greeting - are you there?
                    socket.send_string(message)
                else:
                    socket.send_string('Camera ' + message + ' not recognised')
            except zmq.error.Again: #no message received in non-blocking mode
                time.sleep(1)
        socket.close()

    # Reload the list of available setups from the server - for choices in the overlay dropdown box (cbSetup)
    def populateSetupList(self, setValue=''):
        setupDirs = next(os.walk(self.baseFolder + '\\ALICE Setups\\.'))[1]
        if setValue == '':
            setValue = self.cbSetup.currentText()
        [self.cbSetup.removeItem(0) for i in range(self.cbSetup.count())]
        setIndex = -1
        for dir in setupDirs:
            if not dir == 'Archive':
                self.cbSetup.addItem(dir)
                if dir == setValue:
                    setIndex = self.cbSetup.count() - 1
        if setIndex > -1:
            self.cbSetup.setCurrentIndex(setIndex)
            
        # Now go through previous few shifts
        self.cbSetup.insertSeparator(self.cbSetup.count())
        date = datetime.now()
        dayBefore = timedelta(days=-1)
        workFolder = '\\Work\\{y}\\{m:02d}\\{d:02d}'
        for i in range(10):
            date = date + dayBefore
            if os.path.isdir(self.baseFolder + workFolder.format(y=date.year, m=date.month, d=date.day)):
                self.cbSetup.addItem(date.strftime('%a %#d %b %Y')) #e.g. Mon 8 Feb 2016
        
    def applyButtonProperties(self, btn, iconFileName, clickFcn, row, col, 
                              colspan=1, toggle=False, tip='', parent=None):
        if toggle:
            btn.setCheckable(True)
        setIcon(btn, iconFileName)
        btn.clicked.connect(clickFcn)
        if parent is None: #add to default layout
            self.layout.addWidget(btn, row, col, 1, colspan)
        else:
            parent.addWidget(btn)
        btn.setToolTip(tip)
    
    def updateListItemIcon(self, screenName, icon):
        screen = self.cameras[screenName]
        setIcon(screen.listItem, icon)
    
    def lvCameras_itemDoubleClicked(self):
        "Move the screen in/out when list item double-clicked."
        self.sldMoveScreen.setValue(1 - self.sldMoveScreen.value())
        
    def lvCameras_itemChanged(self, current, previous):
        if not previous is None:
            lastCamera = previous.text()
            # Save threshold value
            self.config.setValue(lastCamera + 'threshold', self.cameras[lastCamera].threshold)
            self.config.setValue(lastCamera + 'stdev-threshold', self.cameras[lastCamera].stdThreshold)
            if self.options['autoMove'].isChecked():
                self.cameras[lastCamera].moveOut()
        self.selectedCamera = current.text()
        # Parse the camera list to find which switcher(s) to change
        screen = self.cameras[self.selectedCamera]
        self.thresholdReset_clicked()
        muxID = screen.muxID
        if not DEBUG:
            if muxID <= 10: # row 1
                self.videoSwitchers[0].write([232, 0, muxID])
            elif 11 <= muxID <= 20: # row 2
                self.videoSwitchers[0].write([232, 1, muxID - 10])
                self.videoSwitchers[0].write([232, 0, 9])
            else: # row 3
                self.videoSwitchers[2].write([muxID - 20,])
                self.videoSwitchers[0].write([232, 0, 10])
            if not previous is None and self.options['autoMove'].isChecked() and not self.selectedCamera == 'INJ-1':
                self.cameras[self.selectedCamera].moveIn()
        self.overlayIndex = 0
        self.updateOverlayImage()
        self.lblGrid.setPixmap(QtGui.QPixmap(screen.gridImage))
        self.grabbing = True
#        if screen.pvStatus.value == screen.statusIn:
#            status = 1
#        elif screen.pvStatus.value == screen.statusOut:
#            status = -1
#        else:
#            status = 0
#        self.updateBtnMoveScreen(status)

    def useStd_changed(self):
        "Update the threshold value shown in the spin box (different for sum or std)."
        screen = self.cameras[self.selectedCamera]
        threshold = screen.stdThreshold if self.options['useStd'].isChecked() else screen.threshold
        self.thresSpinBox.setValue(threshold)

    def thresholdReset_clicked(self):
        screen = self.cameras[self.selectedCamera]
        screen.threshold = float(self.config.value(self.selectedCamera + '-threshold', 0.03))
        screen.stdThreshold = float(self.config.value(self.selectedCamera + '-stdev-threshold', 0.03))
        self.useStd_changed()
        self.thresholdReset.setEnabled(False)

    def thresholdSpin_changed(self, value):
        screen = self.cameras[self.selectedCamera]
        screen.threshold = value
        self.thresholdReset.setEnabled(True)        

    def updateBtnMoveScreen(self, status):
        "Update the move screen slider, and highlight the in/out text."
        if status == 1: #in
            self.screen_in = True
            self.sldMoveScreen.setValue(1)
            self.inOutLabelsChanged.emit('blue', 'black')
        elif status == -1: #out
            self.screen_in = False
            self.sldMoveScreen.setValue(0)
            self.inOutLabelsChanged.emit('black', 'blue')
        else: #moving
            self.inOutLabelsChanged.emit('black', 'black')

    def updateInOutLabelColours(self, inColour, outColour):
        self.lblScreenIn.setStyleSheet('color: ' + inColour)
        self.lblScreenOut.setStyleSheet('color: ' + outColour)
        
    def eventFilter(self, source, event):
        "Handle events - QLabel doesn't have click or wheel events."
        overImage = source in (self.lblImage, self.lblGrid, self.lblOverlay, self.lblPlayPause)
        evType = event.type()
        if evType == QtCore.QEvent.MouseButtonRelease and overImage:
            # Click image -> play/pause live grabbing
            self.imgTimeList = []
            live = not self.grabbing
            filename = 'Play' if live else 'Pause'
            self.lblPlayPause.setPixmap(QtGui.QPixmap('Icons\\{}.png'.format(filename)))
            self.grabbing = live
            self.lblPlayPause.setVisible(True)
            opacity = QtGui.QGraphicsOpacityEffect()
            opacity.setOpacity(1)
            self.lblPlayPause.setGraphicsEffect(opacity)
            self.fadeTimer = QtCore.QTimer()
            self.fadeTimer.timeout.connect(partial(self.fadeButtonMore, datetime.now()))
            self.fadeTimer.start(5)
        elif evType == QtCore.QEvent.Wheel and overImage and self.btnNextOverlay.isEnabled():
            # Mouse wheel over image -> cycle through overlay images
            if event.delta() < 0:
                self.btnNextOverlay_clicked()
            else:
                self.btnPrevOverlay_clicked()
        elif source == self.lblScreenIn and evType == QtCore.QEvent.MouseButtonRelease:
            self.sldMoveScreen.setValue(1)
        elif source == self.lblScreenOut and evType == QtCore.QEvent.MouseButtonRelease:
            self.sldMoveScreen.setValue(0)
        return QtGui.QMainWindow.eventFilter(self, source, event)

    def fadeButtonMore(self, startTime):
        "Nice smooth fadeout of the play/pause image."
        graphicsEffect = self.lblPlayPause.graphicsEffect()
        fade_time = 1 #seconds
        time_since_start = (datetime.now() - startTime).total_seconds()
        graphicsEffect.setOpacity(max(0, 1 - time_since_start / fade_time))
        if time_since_start > fade_time:
            self.fadeTimer.stop()

    def btnLEDs_clicked(self):
        on = self.btnLEDs.isChecked()
        Thread(target=ledsOnOff, args=(self.pvLEDs, on)).start()
    
    def ledStatusChanged(self, pvname=None, value=None, char_value=None, **kw):
        "Called when one of the LED PVs changes status."
        anyOn = False
        self.pvLEDs[pvname[:3]]['status'].really_connected = True
        for led in self.pvLEDs.values():
            if led['status'].really_connected:
                anyOn = anyOn or led['status'].value == 1
        self.ledIconChanged.emit(anyOn)
        
    def updateLEDIcon(self, anyOn):
        iconFileName = 'LEDs_' + ('On' if anyOn else 'Off')
        setIcon(self.btnLEDs, iconFileName)
        self.btnLEDs.setChecked(anyOn)
    
    def btnMoveScreen_clicked(self):
        value = self.sldMoveScreen.value()
        self.screen_in = value == 1
        #which screen selected?
        items = self.lvCameras.selectedItems()
        screenName = items[0].text()
        self.grabbing = self.screen_in
        if self.screen_in:
            self.cameras[screenName].moveIn()
        else:
            self.cameras[screenName].moveOut()

        # Also force an update of screen status (since it might not have changed)
#        pv = self.cameras[screenName].pvStatus
#        self.screenStatusChanged(screenName, pv.pvname, pv.value)
            
    def screenStatusChanged(self, screenName, pvname=None, value=None, char_value=None, **kw):
        screen = self.cameras[screenName]
        if value >= screen.statusIn:
            self.listItemIconChanged.emit(screenName, 'eye')
            status = 1
            # Send a quick update to notify that the screen has gone in
            self.dataPublisher.send_pyobj({'screen': screenName})
        elif value <= screen.statusOut:
            self.listItemIconChanged.emit(screenName, '') #no icon
            status = -1
        else: #assume moving
            self.listItemIconChanged.emit(screenName, 'hourglass')
            status = 0
            
        if screen.listItem.isSelected():
            self.updateBtnMoveScreen(status)
        
    def btnMinTL_clicked(self):
        self.pvTrainLengthSet.put(0.024)
        self.pvTrainLengthSet.poll()
        self.pvTrainLengthRead.poll()
    def btnReduceTL_clicked(self):
        i = np.argmin(self.trainLengthStops < round(self.pvTrainLengthRead.value, 3))
        if i > 0:
            self.pvTrainLengthSet.put(self.trainLengthStops[i - 1])
            self.pvTrainLengthSet.poll()
            self.pvTrainLengthRead.poll()
    def btnIncreaseTL_clicked(self):
        i = np.argmin(self.trainLengthStops <= round(self.pvTrainLengthRead.value, 3))
        if i < len(self.trainLengthStops):
            self.pvTrainLengthSet.put(self.trainLengthStops[i])
            self.pvTrainLengthSet.poll()
            self.pvTrainLengthRead.poll()
    def trainLengthChanged(self, pvname=None, value=None, char_value=None, **kw):
        if value < 1:
            tlText = '{:.0f} ns'.format(value * 1000)
        else:
            tlText = '{:.0f} µs'.format(value)
        self.lblTrainLength.setText(tlText)
        self.btnMinTL.setChecked(value == 0.024)
            
    def chkOverlay_clicked(self):
        checked = self.chkOverlay.isChecked()
        self.lblOverlay.setVisible(checked)
        self.lblOverlayFileName.setVisible(checked)
        self.btnPrevOverlay.setEnabled(checked)
        self.btnNextOverlay.setEnabled(checked)
        
    def chkGrid_clicked(self):
        checked = self.chkGrid.isChecked()
        self.lblGrid.setVisible(checked)

    def getSetupFolder(self, setup):
        try: #have we selected a work folder?
            date = time.strptime(setup, '%a %d %b %Y') #e.g. Mon 8 Feb 2016
            setupFolder = self.baseFolder + '\\Work\\{y}\\{m:02d}\\{d:02d}'.format(y=date.tm_year, m=date.tm_mon, d=date.tm_mday)
        except: #doesn't match - must be a setup folder
            setupFolder = self.baseFolder + '\\ALICE Setups\\' + setup
        return setupFolder
        
    def updateOverlayFiles(self):
        setupFolder = self.getSetupFolder(self.cbSetup.currentText())
        dirListing =  os.listdir(setupFolder)
        #we just want the .png files
        pngList = [f for f in dirListing if f.lower().endswith('.png')]
        #file format is e.g. 0837 INJ-1 500ns.png
        #second space-delimited field is camera name
        #add list of overlay images to camera objects
        for cam in self.cameras.values():
            cam.overlayFiles = [f for f in pngList if (f+' ').split(' ')[1] == cam.name]
        self.overlayIndex = 0
        self.updateOverlayImage()
        
    def updateOverlayImage(self):
        setupFolder = self.getSetupFolder(self.cbSetup.currentText())
        overlayFiles = self.cameras[self.selectedCamera].overlayFiles
        if len(overlayFiles) > 0:
            fileName = setupFolder + '\\' + overlayFiles[self.overlayIndex]
            self.lblOverlay.setPixmap(QtGui.QPixmap(fileName))
            self.lblOverlayFileName.setText('{} ({}/{})'.format(
                overlayFiles[self.overlayIndex], self.overlayIndex + 1, len(overlayFiles)))
        else:
            self.lblOverlay.setPixmap(QtGui.QPixmap())
            self.lblOverlayFileName.setText('(none)')
        
    def cbSetup_clicked(self, index):
        #different setup selected - scan the setup folder for PNG files
        self.updateOverlayFiles()
        self.chkOverlay.setChecked(True)
        self.chkOverlay_clicked()

    def btnPrevOverlay_clicked(self):
        self.advanceOverlay(-1)
    def btnNextOverlay_clicked(self):
        self.advanceOverlay(1)
    def advanceOverlay(self, delta):
        files = self.cameras[self.selectedCamera].overlayFiles
        self.overlayIndex = np.mod(self.overlayIndex + delta, len(files))
        self.updateOverlayImage()
        
    def btnReset_clicked(self):
        self.txtFileName.setText(time.strftime(r'%Y\%m\%d\%H%M [cam] [tl].png'))

    def replaceParams(self, fileName, i=-1):
        fileName = fileName.replace('[cam]', self.selectedCamera)
        tlStr = self.lblTrainLength.text()
        fileName = fileName.replace('[tl]', tlStr)
        # how many digits to store in sequence filenames?
        if i >= 0:
            try:
                strFormat = '{{:0{}d}}'.format(int(np.log10(len(self.imgSequence)) + 1))
            except:
                strFormat = '{:02d}'
            fileName = fileName.replace('[i]', strFormat.format(i))
        fileName = self.baseFolder + '\\Work\\' + fileName
        return fileName

    # This is called from gotImageSequence with an extra argument
    def btnSave_clicked(self, i=-1):
        if type(i) is bool: #get a 'checked' value when the button is clicked - we can ignore it
            i = -1            
        fileName = self.replaceParams(self.txtFileName.text(), i)
        folder, baseName = os.path.split(fileName)
        if not os.path.exists(folder):
            os.makedirs(folder)
        self.image.save(fileName)
        if i < 0: #not saving a sequence - update the saved image display
            pixmap = QtGui.QPixmap(fileName).scaled(self.lblSavedImage.size(), QtCore.Qt.KeepAspectRatio)
            self.lblSavedImage.setPixmap(pixmap)
            self.lblSavedImage.fileName = fileName #so we know what to run when it's clicked
            self.lblSavedImageCaption.setCursor(hand)
    #        caption = self.lblCaption.text().split('<br>')
            files = os.listdir(folder)
            n_pngs = len([name[-4:] == '.png' for name in files])
            self.lblSavedImageCaption.setText(baseName + ' ({0} / {0})'.format(n_pngs))#caption[0] + '<br>' + self.lblTrainLength.text())
            self.displayedImage = baseName
            labelText = 'Saved as <a href="file:///' + folder.replace('\\', '/') + '/' + baseName + '">' + baseName + '</a>'
            self.lblOverlayFileName.setText(labelText)
            self.lblOverlayFileName.setVisible(True)
        else: # saving a sequence
            self.lblOverlayFileName.setText('Saved image {} of {}'.format(i + 1, len(self.imgSequence)))

    def lblSavedImage_clicked(self, event):
        os.system('start "" "' + self.lblSavedImage.fileName + '"')
    
    def lblSavedImage_wheel(self, event):
        print(event.delta())
#        folder, baseName = os.path.split(lblSavedImage.fileName)
#        files = os.listdir(folder)
#        pngs = [name for name in files if name[-4:] == '.png']
#        try:
#            i = (pngs.index(self.displayedImage) + 1) % len(pngs)
#        except ValueError:
#            i = 0
#        self.displayedImage = pngs[i]
#        self.lblSavedImage.set

        #TODO: cycle between images in the work folder

    def lblOverlayFileName_linkClicked(self, url):
        os.system('start "" "' + url + '"')

    def btnHelp_clicked(self):
        webbrowser.open('http://projects.astec.ac.uk/ERLPManual/index.php/Python_Image_Capture')

    def grabImage(self):
        "Grab images continuously in a separate thread."
        have_beam = True
        imgI = 0
        self.time0 = datetime.now()
#        self.dosArray = []
        # slow frame rate for debugging to avoid epilepsy
        frame_interval = .1 if DEBUG else 0.04
        while not self.shutdown:
            if self.grabbing:
                if not DEBUG:
#                    imgArray = np.empty((self.imgHeight, self.imgWidth), np.uint8)
                    self.grablib.IC_SnapImage(self.hGrabber, 100) # 100 ms timeout
                    imgPtr = self.grablib.IC_GetImagePtr(self.hGrabber)
                    #imgPtr is returned as a char pointer, which will screw things up
                    imgPtr2 = ctypes.cast(imgPtr, ctypes.POINTER(ctypes.c_uint8))
                    imgArray = np.ctypeslib.as_array(imgPtr2, (self.imgHeight, self.imgWidth))
                else:
                    t = time.clock()
                    if have_beam:
                        imgI = (imgI + 1) % len(self.testImages)#np.random.randint(0, len(self.testImages) - 1)
                        imgArray = np.copy(self.testImages[imgI])
                    else:
                        imgArray = np.zeros([self.imgHeight, self.imgWidth])
                    have_beam = not have_beam
                    dt = time.clock() - t
#                    print(dt)
                    time.sleep(frame_interval - dt)
                # Another thread to actually process images
                # pass camera info in case it gets changed before we process it
                args = (imgArray, datetime.now(), self.cameras[self.selectedCamera])
                if self.options['threading'].isChecked():
                    Thread(target=self.processImage, args=args).start()
                else: # run serially
                    self.processImage(*args)
            else:
                time.sleep(frame_interval) 

    def processImage(self, imgArray, imgTime, cam):
        "Carry out image processing and display. Run serially or in a separate thread."
        # calculate x and y profiles
        # ix = intensity vs x, i.e. sum of vertical lines
#        tt = time.clock()
        formatString = '<h1 style="margin: 0px">{}</h1><p style="margin: 0px">{}<br>{:.2g} Hz</p>'
        # use sum or std to calculate profiles? may be easier to fit to std
        profFunc = np.std if self.options['useStd'].isChecked() else np.sum
        ix0 = profFunc(imgArray[::2, self.xr], 0, dtype=np.float)
        ix1 = profFunc(imgArray[1::2, self.xr], 0, dtype=np.float)

        # choose integral of odd or even field
        sx0 = np.sum(ix0)
        sx1 = np.sum(ix1)
        diff_over_sum = (sx0 - sx1) / (sx0 + sx1) if sx0 + sx1 > 0 else 0
#        self.dosArray.append(abs(diff_over_sum))
#        if len(self.dosArray) > 20:
#            del(self.dosArray[0])
#            print('{} {:.4f} {:.4f}'.format(self.selectedCamera, np.max(self.dosArray), np.mean(self.dosArray)))
        if self.options['beamOnly'].isChecked():
            threshold = cam.stdThreshold if self.options['useStd'].isChecked() else cam.threshold
        else: 
            threshold = 0
            
        if diff_over_sum > threshold:
            offset = 0
            ix = ix0 - ix1
        elif diff_over_sum < -threshold:
            offset = 1
            ix = ix1 - ix0
        else: # no beam!
            dt = imgTime - self.imgTime
            if dt.total_seconds() > 1:
                self.captionChanged.emit(formatString.format(cam.name, '(no beam)', 0))
            return

#        print('Chosen field', time.clock() - tt)        
        # for vertical profile, subtract the no-beam field from the beam field
        iy = profFunc(imgArray[range(offset, self.imgHeight, 2)], 1, dtype=np.float) - profFunc(imgArray[range(1 - offset, self.imgHeight, 2)], 1, dtype=np.float)
        iy = iy[::-1] # otherwise plot is upside-down

        if self.options['deinterlace'].isChecked():
            imgArray[(1-offset)::2] = imgArray[offset::2]
        elif self.options['subtractDim'].isChecked():
            # Need to be careful about where the dim frame is brighter - otherwise overflows will happen - we want to clip at zero
            overflow = imgArray[(1-offset)::2] > imgArray[offset::2]
            diff = imgArray[offset::2] - imgArray[(1-offset)::2]
            diff[overflow] = 0
            imgArray[offset::2] = diff
            imgArray[(1-offset)::2] = diff

        # Display the image and calculate the frames per second value (to be displayed later)
        with self.threadLock:
            self.image = QtGui.QImage(imgArray, self.imgWidth, self.imgHeight, QtGui.QImage.Format_Indexed8)
            self.image.setColorTable(self.jet)
            self.imgTimeList.append(imgTime)
            if len(self.imgTimeList) > 20:
                del(self.imgTimeList[0])
            if len(self.imgTimeList) > 1:
                freq = (len(self.imgTimeList) - 1) / (self.imgTimeList[-1] - self.imgTimeList[0]).total_seconds()
            else:
                freq = 0
            if self.seqI >= 0: #recording a sequence
                self.imgSequence[self.seqI] = self.image
                self.imgSequence[self.seqI].imgTime = imgTime
                self.lblOverlayFileName.setText('Stored image {} of {}'.format(self.seqI + 1, len(self.imgSequence)))
                self.seqI += 1
                if self.seqI == len(self.imgSequence):
                    self.seqI = -1
                    self.gotImageSequence()
        self.imageChanged.emit()
        self.plotsChanged.emit(ix, iy)

        # fit Gaussian to profiles
        if self.options['fitGaussians'].isChecked():
            x0 = xw = xh = np.nan
            lblDataTemplate = 'Centre:\t{0:.3g} {2}\nFWHM:\t{1:.3g} {2}\nAmplitude: {3:.3g}'
            mod = GaussianModel()
            # Code is basically same for H & V profiles, might as well reuse it
            for i, prof, coord_range in zip((0, 1), (ix, iy), (cam.xrd, cam.yrd)):
                try:
                    with self.threadLock: # fitting is not thread-safe, we need to lock around it
                        initCoeffs = mod.guess(prof, x=coord_range)
                        # If we need more than 30 calls, we probably don't have a beam
                        out = mod.fit(prof, initCoeffs, x=coord_range, fit_kws={'maxfev': 30})
                    params = out.best_values
                    x0 = params['center']
                    xw = params['sigma'] * 2.3548 # FWHM
                    xh = params['amplitude']
                except Exception as e:
                    print(e)
                    pass

                minX = coord_range[0]
                maxX = coord_range[-1]
                if x0 >= minX and x0 <= maxX and xw <= maxX - minX:#xi in self.xr:
                    self.fitChanged.emit(i, out.best_fit, lblDataTemplate.format(x0, xw, cam.units, xh))
                    if self.options['outputToEPICS'].isChecked() and not DEBUG:
                        cam.setPositionPVs(i, x0, xw)
                else:
                    x0 = xw = np.nan
                    self.fitChanged.emit(i, np.zeros(0), 'No fit')
        else:
            [self.fitChanged.emit(i, np.zeros(0), '') for i in range(2)]
        
        del(imgArray) #to free memory
        
        t = datetime.now()
        dt = t - imgTime
        self.imgTime = t
        timeString = t.strftime('%H:%M:%S.%f')
        caption = formatString.format(self.selectedCamera, timeString[:12], freq)
        self.captionChanged.emit(caption) # show ms, not µs
#        mem_top()
        
#        self.lblData.setText(datetime.now().time().isoformat())
#        print('Finished processing', time.clock() - tt)        
        
    # Need to do these on the GUI thread - will respond to a signal
    def updateCaption(self, caption):
        self.lblCaption.setText(caption)
        
    def updateImage(self):
        self.lblImage.setPixmap(QtGui.QPixmap.fromImage(self.image))

    def updatePlots(self, xProf, yProf):
        cam = self.cameras[self.selectedCamera]
        for i, graph in enumerate(self.profilePlot):
            graph.plotItem.clear()
            args = (cam.xrd, xProf) if i==0 else (yProf, cam.yrd)
            graph.plot(*args)
            graph.getViewBox().autoRange(padding=0)
#        self.yProfilePlot.plot(yProf, self.yr)

    def updateFit(self, ax, fit, caption): #ax 0 = x, ax 1 = y
        cam = self.cameras[self.selectedCamera]
        args = (cam.xrd, fit) if ax==0 else (fit, cam.yrd)
        if len(fit) > 0: #pass zero-length array if no fit found
            self.profilePlot[ax].plot(*args, pen=1)
        self.lblFitData[ax].setText(caption)

    def startSequence(self, seqType):
        "Starts saving a sequence of images either as still frames or a movie."
        self.seqType = seqType
        nFrames = int(self.config.value('nFrames', 100))
        if self.seqType == 'png':
            title = 'Record image sequence'
            prompt = 'Number of images to save:'
        else:
            title = 'Record movie'
            prompt = 'Number of movie frames'
            
        nFrames, okClicked = QtGui.QInputDialog.getInt(self, title, prompt, value=nFrames, min=2)
        if okClicked:
            self.lblOverlayFileName.setVisible(True)
            self.config.setValue('nFrames', nFrames)
            self.imgSequence = [QtGui.QImage(self.imgQSize) for i in range(nFrames)]
            self.seqI = 0
            if self.seqType == 'png':
                self.txtFileName.setText(time.strftime(r'%Y\%m\%d\%H%M\[cam] [tl] [i].png'))
    
    def gotImageSequence(self):
        "Saving sequence is complete (called from processImage)."
        fileName = self.txtFileName.text()
        folder, baseName = os.path.split(fileName)
        if self.seqType == 'png':
            for i in range(len(self.imgSequence)):
                self.image = self.imgSequence[i]
                self.btnSave_clicked(i)
            link = '<a href="file:///' + folder.replace('\\', '/') + '/">sequence</a>'
            self.imageLabelChanged.emit('Saved {} of {} images'.format(link, len(self.imgSequence)))
            self.imgSequence = []
        else: #movie
            Thread(target=self.makeMovie).start() #do it in background so we can continue grabbing frames

    def makeMovie(self):
        "Turn a sequence of images into a movie using FFMPEG."
        fileName = self.replaceParams(self.txtFileName.text())
        self.imageLabelChanged.emit('Converting to movie...')
        print(self.imgSequence[-1].imgTime, self.imgSequence[0].imgTime)
        dt = (self.imgSequence[-1].imgTime - self.imgSequence[0].imgTime)
        fps = len(self.imgSequence) / dt.total_seconds()
        print(fps, 'fps')
        # Open an ffmpeg process
        fileName = fileName.rsplit('.png', 1)[0] + '.mp4'
        folder, baseName = os.path.split(fileName)
        if not os.path.exists(folder):
            os.makedirs(folder)
        cmdstring = (self.ffmpegPath, 
            '-y', '-r', '{:.3f}'.format(fps), # overwrite
            '-s', '%dx%d' % (self.imgWidth, self.imgHeight), # size of image string
            '-pix_fmt', 'rgb24', # format
            '-f', 'rawvideo',  '-i', '-', # tell ffmpeg to expect raw video from the pipe
            '-vcodec', 'h264', fileName) # output encoding
        # Hide the ffmpeg console window
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW            
        with subprocess.Popen(cmdstring, stdin=subprocess.PIPE, startupinfo=startupinfo) as proc:
            for frame in self.imgSequence:
                # extract the image as an RGB string
                rgbImage = frame.convertToFormat(QtGui.QImage.Format_RGB888)
                ptr = rgbImage.bits()
                ptr.setsize(rgbImage.numBytes())
                # write to pipe
                proc.stdin.write(bytes(ptr))
        
        link = '<a href="file:///' + folder.replace('\\', '/') + '/' + baseName + '">' + baseName + '</a>'
        self.imageLabelChanged.emit('Saved movie as ' + link)
        self.imgSequence = []

    # Must do this in GUI thread (since we change link text)
    def updateImageLabel(self, text):
        self.lblOverlayFileName.setText(text)

    def close(self):
        "Close the GUI window and release resources."
        #after we've finished, release the grabber
#        print('closing')
        self.config.setValue('windowPosn', self.pos())
        self.config.setValue('windowSize', self.size())
        self.config.setValue('selectedScreen', self.selectedCamera)
        for opt, chk in self.options.items():
            self.config.setValue(opt, chk.isChecked())
        self.shutdown = True
        self.serverThread.join()
        self.grabThread.join()
        if not DEBUG:
            self.grablib.IC_StopLive(self.hGrabber)
            self.grablib.IC_CloseVideoCaptureDevice(self.hGrabber)
            self.grablib.IC_ReleaseGrabber(ctypes.byref(self.hGrabber))
            self.grablib.IC_CloseLibrary()


if __name__ == "__main__":
    app = QtGui.QApplication(sys.argv)

    # Create and display the splash screen
    splash_pix = QtGui.QPixmap('hourglass_256.png')
    splash = QtGui.QSplashScreen(splash_pix, QtCore.Qt.WindowStaysOnTopHint)
    splash.setMask(splash_pix.mask())
    splash.show()
    app.processEvents()

    window = Window()
    app.installEventFilter(window)
    app.aboutToQuit.connect(window.close)
    window.show()
    splash.finish(window)
    sys.exit(app.exec_())
