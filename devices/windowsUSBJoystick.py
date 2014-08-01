# Based on code by rdb released under the Unlicense (unlicense.org)
# Further reading about the WinMM Joystick API:
# http://msdn.microsoft.com/en-us/library/windows/desktop/dd757116(v=vs.85).aspx

from math import floor, ceil
import time
import ctypes
import threading
import device
import events
import interfaces.stageMover
import _winreg as winreg
from ctypes.wintypes import WORD, UINT, DWORD
from ctypes.wintypes import WCHAR as TCHAR

CLASS_NAME = 'WindowsJoystickDevice'

# Fetch function pointers
joyGetNumDevs = ctypes.windll.winmm.joyGetNumDevs
joyGetPos = ctypes.windll.winmm.joyGetPos
joyGetPosEx = ctypes.windll.winmm.joyGetPosEx
joyGetDevCaps = ctypes.windll.winmm.joyGetDevCapsW

# Define constants
MAXPNAMELEN = 32
MAX_JOYSTICKOEMVXDNAME = 260

JOY_RETURNX = 0x1
JOY_RETURNY = 0x2
JOY_RETURNZ = 0x4
JOY_RETURNR = 0x8
JOY_RETURNU = 0x10
JOY_RETURNV = 0x20
JOY_RETURNPOV = 0x40
JOY_RETURNBUTTONS = 0x80
JOY_RETURNRAWDATA = 0x100
JOY_RETURNPOVCTS = 0x200
JOY_RETURNCENTERED = 0x400
JOY_USEDEADZONE = 0x800
JOY_RETURNALL = JOY_RETURNX | JOY_RETURNY | JOY_RETURNZ | JOY_RETURNR | JOY_RETURNU | JOY_RETURNV | JOY_RETURNPOV | JOY_RETURNBUTTONS

# This is the mapping for my XBox 360 controller.
button_names = ['a', 'b', 'x', 'y', 'tl', 'tr', 'back', 'start', 'thumbl', 'thumbr']
povbtn_names = ['dpad_up', 'dpad_right', 'dpad_down', 'dpad_left']

# Define some structures from WinMM that we will use in function calls.
class JOYCAPS(ctypes.Structure):
    _fields_ = [
        ('wMid', WORD),
        ('wPid', WORD),
        ('szPname', TCHAR * MAXPNAMELEN),
        ('wXmin', UINT),
        ('wXmax', UINT),
        ('wYmin', UINT),
        ('wYmax', UINT),
        ('wZmin', UINT),
        ('wZmax', UINT),
        ('wNumButtons', UINT),
        ('wPeriodMin', UINT),
        ('wPeriodMax', UINT),
        ('wRmin', UINT),
        ('wRmax', UINT),
        ('wUmin', UINT),
        ('wUmax', UINT),
        ('wVmin', UINT),
        ('wVmax', UINT),
        ('wCaps', UINT),
        ('wMaxAxes', UINT),
        ('wNumAxes', UINT),
        ('wMaxButtons', UINT),
        ('szRegKey', TCHAR * MAXPNAMELEN),
        ('szOEMVxD', TCHAR * MAX_JOYSTICKOEMVXDNAME),
    ]

class JOYINFO(ctypes.Structure):
    _fields_ = [
        ('wXpos', UINT),
        ('wYpos', UINT),
        ('wZpos', UINT),
        ('wButtons', UINT),
    ]

class JOYINFOEX(ctypes.Structure):
    _fields_ = [
        ('dwSize', DWORD),
        ('dwFlags', DWORD),
        ('dwXpos', DWORD),
        ('dwYpos', DWORD),
        ('dwZpos', DWORD),
        ('dwRpos', DWORD),
        ('dwUpos', DWORD),
        ('dwVpos', DWORD),
        ('dwButtons', DWORD),
        ('dwButtonNumber', DWORD),
        ('dwPOV', DWORD),
        ('dwReserved1', DWORD),
        ('dwReserved2', DWORD),
    ]


class WindowsJoystickDevice(device.Device):
    def __init__(self):
        self.isActive = True
        self.priority = 100
        # Get the number of supported devices (usually 16).
        num_devs = joyGetNumDevs()
        # Number of the joystick to open.
        joy_id = 0
        # Check if the joystick is plugged in.
        self.info = JOYINFO()
        self.p_info = ctypes.pointer(self.info)
        # Get device capabilities.
        self.caps = JOYCAPS()
        # Fetch the name from registry.
        self.key = None
        if len(self.caps.szRegKey) > 0:
            try:
                self.key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "System\\CurrentControlSet\\Control\\MediaResources\\Joystick\\%s\\CurrentJoystickSettings" % (self.caps.szRegKey))
            except WindowsError:
                self.key = None

        if self.key:
            self.oem_name = winreg.QueryValueEx(self.key, "Joystick%dOEMName" % (joy_id + 1))
            if self.oem_name:
                key2 = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "System\\CurrentControlSet\\Control\\MediaProperties\\PrivateProperties\\Joystick\\OEM\\%s" % (self.oem_name[0]))
                if key2:
                    self.oem_name = winreg.QueryValueEx(key2, "OEMName")
                key2.Close()

        # Set the initial button states.
        self.button_states = {}
        for b in range(self.caps.wNumButtons):
            name = button_names[b]
            if (1 << b) & self.info.wButtons:
                self.button_states[name] = True
            else:
                self.button_states[name] = False

        for name in povbtn_names:
            self.button_states[name] = False

        # Initialise the JOYINFOEX structure.
        self.info = JOYINFOEX()
        self.info.dwSize = ctypes.sizeof(JOYINFOEX)
        self.info.dwFlags = JOY_RETURNBUTTONS | JOY_RETURNCENTERED | JOY_RETURNPOV | JOY_RETURNU | JOY_RETURNV | JOY_RETURNX | JOY_RETURNY | JOY_RETURNZ
        self.p_info = ctypes.pointer(self.info)

    
    def initialize(self):
        ## Prepare a thread the reads joystick data and dispatches move events accordingly.
        self.joystickThread = threading.Thread(target = self.readJoystickThread)
        events.subscribe('cockpit initialization complete', self.start)


    def start(self):
        self.joystickThread.start()


    def readJoystickThread(self):
        # Fetch new joystick data until it returns non-0 (that is, it has been unplugged)
        buttons_text = " "

        x_threshold = 0.05
        y_threshold = 0.05


        while joyGetPosEx(0, self.p_info) == 0:
            # Remap the values to float
            x = (self.info.dwXpos - 32767) / 32768.0
            y = (self.info.dwYpos - 32767) / 32768.0
            trig = (self.info.dwZpos - 32767) / 32768.0
            rx = (self.info.dwRpos - 32767) / 32768.0
            ry = (self.info.dwUpos - 32767) / 32768.0

            # NB.  Windows drivers give one axis for the trigger, but I want to have
            # two for compatibility with platforms that do support them as separate axes.
            # This means it'll behave strangely when both triggers are pressed, though.
            lt = max(-1.0,  trig * 2 - 1.0)
            rt = max(-1.0, -trig * 2 - 1.0)

            # Figure out which buttons are pressed.
            for b in range(self.caps.wNumButtons):
                pressed = (0 != (1 << b) & self.info.dwButtons)
                name = button_names[b]
                self.button_states[name] = pressed

            # Determine the state of the POV buttons using the provided POV angle.
            if self.info.dwPOV == 65535:
                povangle1 = None
                povangle2 = None
            else:
                angle = self.info.dwPOV / 9000.0
                povangle1 = int(floor(angle)) % 4
                povangle2 = int(ceil(angle)) % 4

            for i, btn in enumerate(povbtn_names):
                if i == povangle1 or i == povangle2:
                    self.button_states[btn] = True
                else:
                    self.button_states[btn] = False

            # Format a list of currently pressed buttons.
            prev_len = len(buttons_text)
            buttons_text = " "
            for btn in button_names + povbtn_names:
                if self.button_states.get(btn):
                    buttons_text += btn + ' '

            x = x if (abs(x) > x_threshold) else 0
            y = y if (abs(y) > y_threshold) else 0
            
            if abs(x) > 0 or abs(y) > 0:
                interfaces.stageMover.moveRelative((-10*x, -10*y, 0), shouldBlock=False)


            # Add spaces to erase data from the previous line
            #erase = ' ' * max(0, prev_len - len(buttons_text))

            # Display the x, y, trigger values.
            #print "\r(% .3f % .3f % .3f) (% .3f % .3f % .3f)%s%s" % (x, y, lt, rx, ry, rt, buttons_text, erase),

            #print info.dwXpos, info.dwYpos, info.dwZpos, info.dwRpos, info.dwUpos, info.dwVpos, info.dwButtons, info.dwButtonNumber, info.dwPOV, info.dwReserved1, info.dwReserved2
            time.sleep(0.001)
