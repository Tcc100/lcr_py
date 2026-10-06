import io
import unittest
from contextlib import redirect_stdout
from decimal import Decimal, FloatOperation, localcontext
from unittest.mock import patch

from src.de5000 import DE5000, EOL, MEAS_RES, RAW_DATA_LENGTH, READ_RETRIES
from src import de5000_reader


# Parallel resistance: 123.45 kOhm, secondary RP: 1.234 Ohm, 1 KHz.
PACKET = bytes.fromhex("00 0d e6 50 05 03 30 39 12 00 03 04 d2 0b 00 0d 0a")


class DE5000Tests(unittest.TestCase):
    def setUp(self):
        serial_patch = patch('src.de5000.serial.Serial')
        self.serial_factory = serial_patch.start()
        self.addCleanup(serial_patch.stop)
        self.serial = self.serial_factory.return_value
        self.serial.read_until.return_value = PACKET
        self.meter = DE5000('/dev/test')
        self.addCleanup(self.meter.close)

    def test_serial_settings_and_raw_bytes(self):
        self.assertTrue(self.serial.dtr)
        self.assertFalse(self.serial.rts)
        self.assertEqual(self.meter.read_raw_data(), PACKET)
        self.serial.reset_input_buffer.assert_called_once_with()
        self.serial.read_until.assert_called_once_with(EOL, RAW_DATA_LENGTH)

    def test_packet_framing(self):
        for packet in (b'', PACKET[:-1], PACKET + b'\x00',
                       b'\x01' + PACKET[1:], PACKET[:-1] + b'\x00'):
            with self.subTest(packet=packet):
                self.assertFalse(self.meter.is_data_valid(packet))
        self.assertTrue(self.meter.is_data_valid(PACKET))

    def test_retry_invalid_packets(self):
        self.serial.read_until.side_effect = [PACKET[:5], b'\xff' * 17, PACKET]
        self.assertEqual(self.meter.read_raw_data(), PACKET)
        self.assertEqual(self.serial.read_until.call_count, READ_RETRIES)

    def test_missing_packet(self):
        self.serial.read_until.return_value = b''
        self.assertEqual(self.meter.get_meas(), MEAS_RES)
        self.assertEqual(self.serial.read_until.call_count, READ_RETRIES)

    def test_measurements_and_flags(self):
        data = self.meter.get_meas()
        self.assertTrue(data['data_valid'])
        self.assertEqual(data['freq'], '1 KHz')
        self.assertEqual(data['tolerance'], '+-1%')
        self.assertEqual(data['main_quantity'], 'Rp')
        self.assertEqual(data['main_val'], Decimal('123.45'))
        self.assertEqual(data['main_units'], 'kOhm')
        self.assertEqual(data['main_norm_val'], Decimal('123450'))
        self.assertEqual(data['main_norm_units'], 'Ohm')
        self.assertEqual(data['sec_quantity'], 'RP')
        self.assertEqual(data['sec_val'], Decimal('1.234'))
        self.assertEqual(data['sec_units'], 'Ohm')
        self.assertEqual(data['sec_norm_val'], data['sec_val'])
        for key in ('ref_shown', 'delta_mode', 'lcr_auto', 'auto_range', 'parallel'):
            self.assertTrue(data[key], key)
        for key in ('cal_mode', 'sorting_mode'):
            self.assertFalse(data[key], key)

    def test_signed_and_unsigned_secondary_values(self):
        for units, info, raw_value, expected in (
            ('deg', 0x70, b'\xff\xff', -1),
            ('deg', 0x71, b'\x80\x00', Decimal('-3276.8')),
            ('deg', 0x70, b'\x12\x34', 4660),
            ('%', 0x69, b'\xff\x9c', -10),
            ('Ohm', 0x08, b'\xff\xff', 65535),
        ):
            with self.subTest(units=units, raw_value=raw_value):
                packet = bytearray(PACKET)
                packet[11:13] = raw_value
                packet[13] = info
                self.serial.read_until.return_value = bytes(packet)
                data = self.meter.get_meas()
                self.assertEqual(data['sec_units'], units)
                self.assertEqual(data['sec_val'], expected)

    def test_unknown_tolerance_preserves_measurement(self):
        for tolerance in (0x40, 0x0b, 0xff):
            with self.subTest(tolerance=tolerance):
                packet = bytearray(PACKET)
                packet[4] = tolerance
                self.serial.read_until.return_value = bytes(packet)
                data = self.meter.get_meas()
                self.assertTrue(data['data_valid'])
                self.assertIsNone(data['tolerance'])
                self.assertEqual(data['main_val'], Decimal('123.45'))
                self.assertEqual(data['sec_val'], Decimal('1.234'))

    def test_decimal_values_and_exact_small_unit_normalization(self):
        packet = bytearray(PACKET)
        packet[6:8] = b'\x00\x1d'  # 29, scaled to 0.29 pF
        packet[8] = 0x4a
        packet[11:13] = b'\x00\x1d'
        packet[13] = 0x0a  # 0.29 Ohm
        self.serial.read_until.return_value = bytes(packet)
        with localcontext() as context:
            context.traps[FloatOperation] = True
            data = self.meter.get_meas()
        for field in ('main_val', 'main_norm_val', 'sec_val', 'sec_norm_val'):
            self.assertIsInstance(data[field], Decimal)
        self.assertEqual(data['main_val'], Decimal('0.29'))
        self.assertEqual(data['main_norm_val'], Decimal('0.00000000000029'))
        self.assertEqual(data['sec_val'], Decimal('0.29'))
        self.assertEqual(data['sec_norm_val'], Decimal('0.29'))
        with redirect_stdout(io.StringIO()) as output:
            self.meter.pretty_print(disp_norm_val=True)
        self.assertIn('Primary: 0.00000000000029 F', output.getvalue())

    def test_serial_mode_and_capacitance_normalization(self):
        packet = bytearray(PACKET)
        packet[2] = 0
        packet[5] = 2
        packet[8] = 0x52  # nF, two decimal places
        self.serial.read_until.return_value = bytes(packet)
        data = self.meter.get_meas()
        self.assertFalse(data['parallel'])
        self.assertEqual(data['main_quantity'], 'Cs')
        self.assertEqual(data['sec_quantity'], 'ESR')
        self.assertEqual(data['main_units'], 'nF')
        self.assertEqual(data['main_norm_val'], Decimal('123.45e-9'))
        self.assertEqual(data['main_norm_units'], 'F')

    def test_secondary_status_uses_four_bits(self):
        for status, expected in ((8, 'FAIL'), (9, 'OPEn'), (10, 'Srt')):
            with self.subTest(status=status):
                packet = bytearray(PACKET)
                packet[14] = status
                self.serial.read_until.return_value = bytes(packet)
                self.assertEqual(self.meter.get_meas()['sec_status'], expected)

    def test_hex_output(self):
        self.assertEqual(
            self.meter.read_hex_str_data(),
            '0x00 0x0D 0xE6 0x50 0x05 0x03 0x30 0x39 0x12 0x00 '
            '0x03 0x04 0xD2 0x0B 0x00 0x0D 0x0A',
        )

    def test_pretty_print(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.meter.pretty_print(disp_norm_val=True)
        self.assertIn('Frequency: 1 KHz', output.getvalue())
        self.assertIn('Rp = 123.45 kOhm', output.getvalue())
        self.assertIn('RP = 1.234 Ohm', output.getvalue())
        self.assertIn('Primary: 123450 Ohm', output.getvalue())

    def test_context_manager_closes_on_error(self):
        with self.assertRaises(RuntimeError):
            with self.meter:
                raise RuntimeError('read failed')
        self.serial.close.assert_called_once_with()

    def test_monitor_interrupt_closes_serial(self):
        with patch('sys.argv', ['de5000_reader.py', '/dev/test']), \
             patch.object(DE5000, 'pretty_print', side_effect=KeyboardInterrupt), \
             redirect_stdout(io.StringIO()) as output:
            de5000_reader.main()
        self.assertEqual(self.serial_factory.call_args.args[0], '/dev/test')
        self.serial.close.assert_called_once_with()
        self.assertIn('Exiting DE-5000 monitor.', output.getvalue())


if __name__ == '__main__':
    unittest.main()
