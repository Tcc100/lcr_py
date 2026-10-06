'''
Created on Sep 15, 2017

@author: 4x1md

Serial port settings: 9600 8N1 DTR=1 RTS=0
'''

from decimal import Decimal
import struct

import serial

# Settings constants
BAUD_RATE = 9600
BITS = serial.EIGHTBITS
PARITY = serial.PARITY_NONE
STOP_BITS = serial.STOPBITS_ONE
TIMEOUT = 1
# Data packet ends with CR LF (\r \n) characters
EOL = b'\x0D\x0A'
# Big-endian packet: header, flags/config/tolerance, two measurements, footer.
PACKET = struct.Struct(">2sBBBBHBBBHBB2s")
RAW_DATA_LENGTH = PACKET.size
READ_RETRIES = 3

# Cyrustek ES51919 protocol constants
# Byte 0x02: flags
# bit 0 = hold enabled
HOLD = 0b00000001
# bit 1 = reference shown (in delta mode)
REF_SHOWN = 0b00000010
# bit 2 = delta mode
DELTA = 0b00000100
# bit 3 = calibration mode
CAL = 0b00001000
# bit 4 = sorting mode
SORTING = 0b00010000
# bit 5 = LCR mode
LCR_AUTO = 0b00100000
# bit 6 = auto mode
AUTO_RANGE = 0b01000000
# bit 7 = parallel measurement (vs. serial)
PARALLEL = 0b10000000

# Byte 0x03 bits 5-7: Frequency
FREQ = [
    '100 Hz',
    '120 Hz',
    '1 KHz',
    '10 KHz',
    '100 KHz',
    'DC'
]

# Byte 0x04: tolerance
TOLERANCE = [
    None,
    None, None,
    '+-0.25%',
    '+-0.5%',
    '+-1%',
    '+-2%',
    '+-5%',
    '+-10%',
    '+-20%',
    '-20+80%',
]

# Byte 0x05: primary measured quantity (serial and parallel mode)
MEAS_QUANTITY_SER = [None, 'Ls', 'Cs', 'Rs', 'DCR']
MEAS_QUANTITY_PAR = [None, 'Lp', 'Cp', 'Rp', 'DCR']

# Bytes 0x08, 0x0D bits 3-7: Units
MAIN_UNITS = [
    '',
    'Ohm',
    'kOhm',
    'MOhm',
    None,
    'uH',
    'mH',
    'H',
    'kH',
    'pF',
    'nF',
    'uF',
    'mF',
    '%',
    'deg',
    None, None, None, None, None, None
]

# Bytes 0x09, 0x0E bits 0-3: Measurement display status
STATUS = [
    'normal',
    'blank',
    '----',
    'OL',
    None, None, None,
    'PASS',
    'FAIL',
    'OPEn',
    'Srt'
]

# Byte 0x0a: secondary measured quantity
SEC_QUANTITY = [
    None,
    'D',
    'Q',
    'ESR',
    'Theta'
]
RP = 'RP'

# Output format
MEAS_RES = {
    'main_quantity': None,
    'main_val': None,
    'main_units': None,
    'main_status': None,
    'main_norm_val': None,
    'main_norm_units': None,

    'sec_quantity': None,
    'sec_val': None,
    'sec_units': None,
    'sec_status': None,
    'sec_norm_val': None,
    'sec_norm_units': None,

    'freq': None,
    'tolerance': None,
    'ref_shown': False,
    'delta_mode': False,
    'cal_mode': False,
    'sorting_mode': False,
    'lcr_auto': False,
    'auto_range': False,
    'parallel': False,

    'data_valid': False
}

# Normalization constants
# Each value contains multiplier and target value
NORMALIZE_RULES = {
    '': (Decimal('1'), ''),
    'Ohm': (Decimal('1'), 'Ohm'),
    'kOhm': (Decimal('1e3'), 'Ohm'),
    'MOhm': (Decimal('1e6'), 'Ohm'),
    'uH': (Decimal('1e-6'), 'H'),
    'mH': (Decimal('1e-3'), 'H'),
    'H': (Decimal('1'), 'H'),
    'kH': (Decimal('1e3'), 'H'),
    'pF': (Decimal('1e-12'), 'F'),
    'nF': (Decimal('1e-9'), 'F'),
    'uF': (Decimal('1e-6'), 'F'),
    'mF': (Decimal('1e-3'), 'F'),
    '%': (Decimal('1'), '%'),
    'deg': (Decimal('1'), 'deg')
}


class DE5000:

    def __init__(self, port):
        self._port = port
        self._ser = serial.Serial(port, BAUD_RATE, BITS, PARITY, STOP_BITS, timeout=TIMEOUT)
        self._ser.dtr = True
        self._ser.rts = False

    def read_raw_data(self) -> bytes:
        """Reads a new data packet from serial port.
        Returns bytes for a valid packet, or empty bytes if no valid packet
        arrives within READ_RETRIES attempts.

        In order to get the last reading the input buffer is flushed
        before reading any data.
        """
        self._ser.reset_input_buffer()
        for _ in range(READ_RETRIES):
            raw_data = self._ser.read_until(EOL, RAW_DATA_LENGTH)
            if self.is_data_valid(raw_data):
                return raw_data
        return b""

    def is_data_valid(self, raw_data: bytes) -> bool:
        """Checks data validity:
        1. 17 bytes long
        2. Header bytes 0x00 0x0D
        3. Footer bytes 0x0D 0x0A
        """
        return (
                len(raw_data) == RAW_DATA_LENGTH
                and raw_data[:2] == b"\x00\x0d"
                and raw_data[-2:] == EOL
        )

    def read_hex_str_data(self) -> str:
        """Returns raw data represented as string with hexadecimal values."""
        return " ".join(f"0x{value:02X}" for value in self.read_raw_data())

    def get_meas(self) -> dict:
        """Returns received measurement as dictionary."""
        res = MEAS_RES.copy()
        raw_data = self.read_raw_data()
        if not raw_data:
            return res

        (
            _header, flags, config, tolerance,
            main_quantity, main_value, main_info, main_status,
            sec_quantity, sec_value, sec_info, sec_status, _footer,
        ) = PACKET.unpack(raw_data)

        res['freq'] = FREQ[config >> 5]
        res['hold'] = bool(flags & HOLD)
        res['ref_shown'] = bool(flags & REF_SHOWN)
        res['delta_mode'] = bool(flags & DELTA)
        res['cal_mode'] = bool(flags & CAL)
        res['sorting_mode'] = bool(flags & SORTING)
        res['lcr_auto'] = bool(flags & LCR_AUTO)
        res['auto_range'] = bool(flags & AUTO_RANGE)
        res['parallel'] = bool(flags & PARALLEL)

        # Main measurement
        quantities = MEAS_QUANTITY_PAR if res['parallel'] else MEAS_QUANTITY_SER
        res['main_quantity'] = quantities[main_quantity]
        res['main_status'] = STATUS[main_status & 0x0f]
        res['main_units'] = MAIN_UNITS[main_info >> 3]
        res['main_val'] = Decimal(main_value).scaleb(-(main_info & 0x07))
        res['main_norm_val'], res['main_norm_units'] = self.normalize_val(res['main_val'], res['main_units'])

        # Secondary measurement
        res['sec_quantity'] = RP if res['parallel'] and sec_quantity == 3 else SEC_QUANTITY[sec_quantity]
        res['sec_status'] = STATUS[sec_status & 0x0f]
        res['sec_units'] = MAIN_UNITS[sec_info >> 3]
        # Percentages and phase angles use signed, two's-complement values.
        if res['sec_units'] in ('%', 'deg'):
            sec_value = struct.unpack_from(">h", raw_data, 0x0b)[0]
        res['sec_val'] = Decimal(sec_value).scaleb(-(sec_info & 0x07))
        res['sec_norm_val'], res['sec_norm_units'] = self.normalize_val(res['sec_val'], res['sec_units'])

        # Some meter packets report undocumented codes, such as 0x40.
        res['tolerance'] = TOLERANCE[tolerance] if tolerance < len(TOLERANCE) else None
        res['data_valid'] = True
        return res

    def normalize_val(self, val: Decimal, units: str) -> tuple[Decimal, str]:
        """Normalizes measured value to standard units. Resistance
        is normalized to Ohm, capacitance to Farad and inductance
        to Henry. Other units are not changed.
        """
        multiplier, normalized_units = NORMALIZE_RULES[units]
        return val * multiplier, normalized_units

    def pretty_print(self, disp_norm_val=False):
        """Prints measurement details in pretty print.
        disp_norm_val: if True, normalized values will also be displayed.
        """
        data = self.get_meas()
        if not data['data_valid']:
            print("DE-5000 is not connected.")
            return

        # In calibration mode frequency is not displayed.
        if data['cal_mode']:
            print("Calibration")
        else:
            if data['sorting_mode']:
                print(f"SORTING Tol {data['tolerance']}")
            print(f"Frequency: {data['freq']}")

        if data['hold']:
            print("Hold")

        # LCR autodetection mode
        if data['lcr_auto']:
            print("LCR AUTO")

        # Auto range
        if data['auto_range']:
            print("AUTO RNG")

        # Delta mode parameters
        if data['delta_mode']:
            print("DELTA Ref" if data['ref_shown'] else "DELTA")

        # Main display
        if data['main_status'] == 'normal':
            print(f"{data['main_quantity']} = {data['main_val']:f} {data['main_units']}")
        elif data['main_status'] == 'blank':
            print()
        else:
            print(data['main_status'])

        # Secondary display
        if data['sec_status'] == 'normal':
            if data['sec_quantity'] is not None:
                print(f"{data['sec_quantity']} = {data['sec_val']:f} {data['sec_units']}")
            else:
                print(f"{data['sec_val']:f} {data['sec_units']}")
        elif data['sec_status'] == 'blank':
            print()
        else:
            print(data['sec_status'])

        # Display normalized values
        # If measurement status is not normal, ---- will be displayed.
        if disp_norm_val:
            if data['main_status'] == 'normal':
                print(f"Primary: {data['main_norm_val']:f} {data['main_norm_units']}")
            else:
                print("Primary: ----")
            if data['sec_status'] == 'normal':
                print(f"Secondary: {data['sec_norm_val']:f} {data['sec_norm_units']}")
            else:
                print("Secondary: ----")

    def close(self):
        self._ser.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
