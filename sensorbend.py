"""EXPERIMENTAL: bend the IMX219 itself by poking its registers over I2C.

Only volatile sensor registers are touched (no OTP / NVM), every original value
is read back first and restored on mode change and on exit. The kernel driver
also rewrites the whole register set every time streaming restarts, so closing
the app (or a camera restart by the watchdog) always undoes everything.

Known bad: shrinking LINE_LENGTH (0x0162) stops frames entirely - left out.
"""

import fcntl
import os

I2C_SLAVE_FORCE = 0x0706
ADDR = 0x10

# Registers we are allowed to write. Anything else is refused.
WHITELIST = {
    0x0158, 0x0159,          # digital gain (4.8 fixed point)
    0x0172,                  # image orientation (mirror / flip)
    0x0600, 0x0601,          # test pattern mode
    0x0602, 0x0603, 0x0604, 0x0605, 0x0606, 0x0607, 0x0608, 0x0609,  # test colours
    0x0383, 0x0387,          # x / y odd increment (readout skipping)
    0x018C, 0x018D,          # CSI data format
}


def _xor(mask):
    return ("xor", mask)


def presets(seed):
    """Level 1..9 -> (label, [(reg, value_or_op), ...])."""
    r = (seed * 2654435761) & 0xFFFFFFFF
    col = [(r >> s) & 0x3 for s in range(0, 8, 2)]
    lo = [(r >> s) & 0xFF for s in range(8, 32, 8)] + [r & 0xFF]
    return [
        ("DIGITAL GAIN x4", [(0x0158, 0x04), (0x0159, 0x00)]),
        ("DIGITAL GAIN x16", [(0x0158, 0x0F), (0x0159, 0xFF)]),
        ("MIRROR (BAYER SLIP)", [(0x0172, _xor(1))]),
        ("FLIP (BAYER SLIP)", [(0x0172, _xor(2))]),
        ("SKIP READOUT X3", [(0x0383, 3)]),
        ("CSI RAW10->8", [(0x018C, 0x0A), (0x018D, 0x08)]),
        ("TEST: COLOUR BARS", [(0x0600, 0), (0x0601, 2)]),
        ("TEST: PN9 NOISE", [(0x0600, 0), (0x0601, 4)]),
        ("TEST: SOLID #%02X%02X" % (lo[0], lo[1]),
         [(0x0600, 0), (0x0601, 1),
          (0x0602, col[0]), (0x0603, lo[0]), (0x0604, col[1]), (0x0605, lo[1]),
          (0x0606, col[2]), (0x0607, lo[2]), (0x0608, col[3]), (0x0609, lo[3])]),
    ]


def find_bus():
    base = "/sys/bus/i2c/devices"
    try:
        for name in os.listdir(base):
            if name.endswith("-0010"):
                with open(f"{base}/{name}/name") as f:
                    if f.read().strip() == "imx219":
                        return int(name.split("-")[0])
    except OSError:
        pass
    return None


class SensorBend:
    def __init__(self):
        self.fd = None
        self.saved = {}
        self.active_label = None
        bus = find_bus()
        if bus is None:
            raise RuntimeError("IMX219 not found on I2C")
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE_FORCE, ADDR)

    def read(self, reg):
        os.write(self.fd, bytes([reg >> 8, reg & 0xFF]))
        return os.read(self.fd, 1)[0]

    def write(self, reg, val):
        if reg not in WHITELIST:
            raise ValueError("register 0x%04X not whitelisted" % reg)
        os.write(self.fd, bytes([reg >> 8, reg & 0xFF, val & 0xFF]))

    def apply(self, label, pokes):
        self.restore()
        for reg, val in pokes:
            if reg not in self.saved:
                self.saved[reg] = self.read(reg)
            if isinstance(val, tuple):
                val = self.saved[reg] ^ val[1]
            self.write(reg, val)
        self.active_label = label

    def restore(self):
        for reg in reversed(list(self.saved)):
            try:
                self.write(reg, self.saved[reg])
            except OSError:
                pass
        self.saved.clear()
        self.active_label = None

    def forget(self):
        """Stream was restarted: the driver already reset every register."""
        self.saved.clear()
        self.active_label = None

    def close(self):
        if self.fd is not None:
            try:
                self.restore()
            finally:
                os.close(self.fd)
                self.fd = None
