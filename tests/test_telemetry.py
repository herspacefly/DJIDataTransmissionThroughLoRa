"""Hardware-free regression tests; run: python -m unittest discover -s tests -v."""
import builtins
import csv
import importlib.util
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import pico_ch4_bridge as bridge
import mira_ground_station as ground

ROOT = Path(__file__).resolve().parents[1]
TS = '10/14/2026 20:00:00.123'


def source(ch4='2.31506', h2o='15724.4', c2h6='5.41189', lat='nan', lon='No Data'):
    return (','.join([TS, '1', '1', '1', '1', '1', ch4, h2o, c2h6,
                      '1', '1', '1', '1', '1', '1', lat, lon]) + '\n').encode('ascii')


def nmea(body):
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f'${body}*{checksum:02X}'


GGA = nmea('GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,')
RMC = nmea('GPRMC,123519,A,4807.038,N,01131.000,E,0,0,141026,,,A')


class BridgeTests(unittest.TestCase):
    def test_source_precision_missing_values_and_validation(self):
        self.assertEqual(bridge.parse(source('0', 'No Data', '-5.85'))[1:],
                         ('0', 'nan', '-5.85', 'nan', 'nan'))
        self.assertEqual(bridge.parse(source())[1], '2.31506')
        missing_column = b','.join(source().strip().split(b',')[:-1]) + b'\n'
        for raw in (source().replace(b'10/14/2026', b'bad'), missing_column,
                    source(ch4='bad'), source().replace(b'2.31506', b'2.3\xff1506')):
            with self.subTest(raw=raw):
                self.assertIsNone(bridge.parse(raw))

    def test_no_gps_and_instrument_coordinate_fallback(self):
        self.assertTrue(bridge.build_frame(0, bridge.parse(source()), None).endswith(b',nan,nan,nan\n'))
        frame = bridge.build_frame(1, bridge.parse(source(lat='31.230416123', lon='121.473701234')), None)
        self.assertTrue(frame.endswith(b',31.230416123,121.473701234,nan\n'))

    def test_gps_position_altitude_and_checksum(self):
        gps = bridge.GpsReader()
        self.assertTrue(gps._consume(GGA))
        lat, lon, alt = gps.get()
        self.assertAlmostEqual(lat, 48.1173)
        self.assertAlmostEqual(lon, 11.5166666667)
        self.assertEqual(alt, 545.4)
        self.assertTrue(bridge.build_frame(0, bridge.parse(source()), gps).endswith(b',48.117300,11.516667,545.4\n'))
        gps._consume(RMC)
        self.assertEqual(gps.get()[2], 545.4)
        before = gps.get()
        self.assertFalse(gps._consume(GGA[:-2] + '00'))
        self.assertEqual(gps.get(), before)

    def test_no_fix_and_disconnected_gps_clear_old_values(self):
        gps = bridge.GpsReader()
        for invalid in (nmea('GPGGA,123519,,,,,0,00,99.9,,,,,,'),
                        nmea('GPRMC,123519,V,,,,,,,141026,,,N')):
            gps._consume(GGA)
            self.assertTrue(gps._consume(invalid))
            self.assertEqual(gps.get(), (None, None, None))
        gps._consume(GGA)
        gps._clear()
        self.assertEqual(gps.get(), (None, None, None))

    def test_rmc_does_not_refresh_stale_altitude(self):
        gps = bridge.GpsReader()
        with patch.object(bridge.time, 'monotonic', return_value=100):
            gps._consume(GGA)
        with patch.object(bridge.time, 'monotonic', return_value=111):
            gps._consume(RMC)
            self.assertIsNone(gps.get()[2])
            self.assertIsNotNone(gps.get()[0])
        with patch.object(bridge.time, 'monotonic', return_value=122):
            self.assertEqual(gps.get(), (None, None, None))

    def test_coordinate_validation(self):
        for dm, hemi in (('nan', 'N'), ('inf', 'E'), ('4860', 'N'), ('9100', 'N'),
                         ('18100', 'E'), ('-4800', 'N'), ('4800', 'X')):
            self.assertIsNone(bridge.dm2dd(dm, hemi))
        self.assertAlmostEqual(bridge.dm2dd('4807.038', 'S'), -48.1173)
        fields = GGA[1:].split('*')[0].split(',')
        fields[3] = 'E'
        self.assertIsNone(bridge.parse_gga(fields))

    def test_discovery_excludes_serial_aliases_and_non_gps_usb(self):
        infos = [SimpleNamespace(device='/dev/ttyUSB0', vid=0x0403),
                 SimpleNamespace(device='/dev/ttyUSB1', vid=0x1A86),
                 SimpleNamespace(device='/dev/ttyACM0', vid=0x1546)]
        aliases = {bridge.SRC: '/dev/ttyUSB0', bridge.DST: '/dev/ttyUSB1',
                   '/dev/ttyGPS': '/dev/ttyACM0'}
        gps = bridge.GpsReader(['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyGPS'])
        with patch.object(bridge.list_ports, 'comports', return_value=infos), \
                patch.object(bridge.os.path, 'exists', return_value=True), \
                patch.object(bridge.os.path, 'realpath', side_effect=lambda p: aliases.get(p, p)):
            self.assertEqual(gps._candidate_ports(), ['/dev/ttyGPS'])
            infos.pop()
            gps.port = ['/dev/ttyUSB0', '/dev/ttyUSB1']
            self.assertEqual(gps._candidate_ports(), [])

    def test_no_gps_thread_never_opens_other_ports(self):
        gps = bridge.GpsReader(bridge.GPS_PORT)
        with patch.object(bridge.list_ports, 'comports', return_value=[]), \
                patch.object(bridge.os.path, 'exists', return_value=False), \
                patch.object(bridge.serial, 'Serial') as serial_open:
            gps.start()
            gps.stop()
            gps.join(2)
            self.assertFalse(gps.is_alive())
            serial_open.assert_not_called()

    def test_gps_partial_lines_and_no_fix_still_lock_device(self):
        gps = bridge.GpsReader()
        stream = Mock()
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        raw = (GGA + '\r\n').encode('ascii')
        def read(size):
            if read.calls == 0:
                read.calls += 1
                return raw[:20]
            if read.calls == 1:
                read.calls += 1
                return raw[20:]
            gps.stop()
            return b''
        read.calls = 0
        stream.readline.side_effect = read
        with patch.object(bridge.serial, 'Serial', return_value=stream):
            gps._read_port('/dev/ttyACM0', 9600)
        self.assertEqual(gps.get()[2], 545.4)
        self.assertTrue(gps._consume(nmea('GPGGA,123519,,,,,0,00,99.9,,,,,,')))

    def test_oversized_frame_is_rejected_without_truncation(self):
        reading = bridge.parse(source(ch4='1' * 100))
        self.assertIsNone(bridge.build_frame(0, reading, None))

    def run_bridge(self, ports, args=('--no-gps',)):
        with patch.object(bridge, 'open_port', side_effect=ports), \
                patch.object(bridge.logging, 'basicConfig'), \
                patch.object(bridge, 'RotatingFileHandler'), patch.object(builtins, 'print'):
            bridge.main(list(args))

    def test_partial_oversized_source_and_serial_reconnect(self):
        src1, dst1, src2, dst2 = (Mock() for _ in range(4))
        raw = source()
        src1.readline.side_effect = [raw[:25], b'', raw[25:],
                                    serial.SerialException('unplugged')]
        src2.readline.side_effect = [b'x' * 1025, b'tail\n', raw, KeyboardInterrupt()]
        dst1.write.side_effect = dst2.write.side_effect = len
        self.run_bridge([src1, dst1, src2, dst2])
        self.assertTrue(dst1.write.call_args.args[0].startswith(b'$MIRA,0,'))
        self.assertTrue(dst2.write.call_args.args[0].startswith(b'$MIRA,1,'))
        for port in (src1, dst1, src2, dst2):
            port.close.assert_called_once()

    def test_short_write_reconnects_without_advancing_sequence(self):
        src1, dst1, src2, dst2 = (Mock() for _ in range(4))
        src1.readline.side_effect = [source(), KeyboardInterrupt()]
        src2.readline.side_effect = [source(), KeyboardInterrupt()]
        dst1.write.return_value = 1
        dst2.write.side_effect = len
        self.run_bridge([src1, dst1, src2, dst2])
        self.assertEqual(dst1.write.call_args.args[0], dst2.write.call_args.args[0])
        dst1.close.assert_called_once()

    def test_default_mode_sends_without_connected_gps(self):
        src, dst = Mock(), Mock()
        src.readline.side_effect = [source(), KeyboardInterrupt()]
        dst.write.side_effect = len
        with patch.object(bridge.list_ports, 'comports', return_value=[]), \
                patch.object(bridge.os.path, 'exists', return_value=False):
            self.run_bridge([src, dst], args=())
        self.assertTrue(dst.write.call_args.args[0].endswith(b',nan,nan,nan\n'))

    def test_dry_run_does_not_open_lora(self):
        src = Mock()
        src.readline.side_effect = [source(), KeyboardInterrupt()]
        self.run_bridge([src], args=('--dry-run', '--no-gps'))
        src.write.assert_not_called()


# Import serial after bridge to use the same exception type in reconnect tests.
import serial


class GroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mira_test_')
        self.root = ground.tk.Tk()
        self.root.withdraw()
        self.app = ground.GroundStation(self.root)
        self.app.data_dir = lambda: self.temp.name
        self.app.open_csv()

    def tearDown(self):
        self.app.on_close()
        self.temp.cleanup()

    def test_real_widgets_csv_and_all_legacy_frame_formats(self):
        gps = bridge.GpsReader()
        gps._consume(GGA)
        self.app.process_raw(bridge.build_frame(0, bridge.parse(source()), gps), time.time())
        self.assertEqual(self.app.info_vars['高程(m)'].get(), '545.4')
        self.assertEqual(self.app.info_vars['纬度'].get(), '48.117300')
        for seq, suffix in ((1, ''), (2, ',31.2,121.4'), (3, ',nan,,No Data')):
            self.app.handle(f'$MIRA,{seq},{TS},0,No Data,-5.85{suffix}', time.time())
        for suffix in ('', ',31.2,121.4', ',31.2,121.4,10.5'):
            self.app.handle(f'$MIRA,{TS},2,3,4{suffix}', time.time())
        self.assertEqual(self.app.rx_count, 7)
        self.assertEqual(self.app.info_vars['高程(m)'].get(), '10.5')
        with open(self.app.csv_path, encoding='utf-8', newline='') as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0][-1], 'Altitude_MSL_m')
        self.assertTrue(all(len(row) == 9 for row in rows))
        self.assertEqual(rows[1][-3:], ['48.117300', '11.516667', '545.4'])
        self.assertEqual(rows[4][-3:], ['nan', 'nan', 'nan'])
        self.assertEqual(rows[-1][1], '')

    def test_ground_reader_joins_partial_frames_and_recovers_from_oversize(self):
        stop, received = threading.Event(), queue.Queue()
        frame = bridge.build_frame(0, bridge.parse(source()), None)
        port = Mock(is_open=True)
        chunks = iter([frame[:15], b'', frame[15:], b'x' * 122, b'tail\n', frame])
        def read(size):
            try:
                return next(chunks)
            except StopIteration:
                stop.set()
                return b''
        port.readline.side_effect = read
        self.app.read_loop(port, stop, received)
        raws = [received.get_nowait()[0] for _ in range(received.qsize())]
        self.assertEqual(raws, [frame, b'[oversized serial line discarded]\n', frame])

    def test_invalid_frames_do_not_update_values_or_csv(self):
        for frame in (f'$MIRA,0,{TS},bad,3,4,nan,nan,nan',
                      f'$MIRA,0,{TS},2,3,4,nan', '$MIRA,0,bad,2,3,4'):
            self.app.handle(frame, time.time())
        self.assertEqual(self.app.rx_count, 0)


@unittest.skipUnless(os.name == 'nt', 'Windows launcher')
class LauncherTests(unittest.TestCase):
    def test_actual_bat_launches_hidden_gui_and_closes_cleanly(self):
        # A temporary startup hook closes the real UI after verifying construction.
        # Dependency checks and the launcher itself run unchanged; no hardware opens.
        with tempfile.TemporaryDirectory(prefix='mira_bat_test_') as tmp:
            marker = Path(tmp) / 'started.txt'
            hook = '''import os, sys
if os.environ.get('MIRA_LAUNCHER_TEST') == '1' and sys.argv[0].endswith('mira_ground_station.py'):
    import tkinter as tk
    from pathlib import Path
    original_tk = tk.Tk
    original_loop = tk.Misc.mainloop
    def hidden_tk(*args, **kwargs):
        root = original_tk(*args, **kwargs)
        root.withdraw()
        return root
    def checked_loop(root, *args, **kwargs):
        from __main__ import GroundStation
        import gc
        app = next(obj for obj in gc.get_objects() if isinstance(obj, GroundStation))
        assert '高程(m)' in app.info_vars
        assert len(app.vals) == 3
        def close():
            Path(os.environ['MIRA_TEST_MARKER']).write_text('GUI started and closed', encoding='utf-8')
            app.on_close()
        root.after(300, close)
        return original_loop(root, *args, **kwargs)
    tk.Tk = hidden_tk
    tk.Misc.mainloop = checked_loop
'''
            (Path(tmp) / 'sitecustomize.py').write_text(hook, encoding='utf-8')
            env = dict(os.environ, PYTHONPATH=tmp, MIRA_LAUNCHER_TEST='1', MIRA_TEST_MARKER=str(marker))
            result = subprocess.run(['cmd.exe', '/d', '/c', str(ROOT / '启动地面站.bat')],
                                    cwd=tmp, env=env, input='', capture_output=True,
                                    text=True, errors='replace', timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(marker.exists(), result.stdout + result.stderr)

    def test_optional_terminal_dependency_failure_still_builds_gui(self):
        original_import = builtins.__import__
        def without_terminal(name, *args, **kwargs):
            if name in ('winpty', 'pyte'):
                raise ImportError('simulated missing terminal dependency')
            return original_import(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location('ground_without_terminal', ROOT / 'mira_ground_station.py')
        module = importlib.util.module_from_spec(spec)
        with patch.object(builtins, '__import__', side_effect=without_terminal):
            spec.loader.exec_module(module)
        self.assertFalse(module.HAS_PTY)
        root = module.tk.Tk()
        root.withdraw()
        app = module.GroundStation(root)
        try:
            app.handle(f'$MIRA,0,{TS},2,3,4,nan,nan,nan', time.time())
            self.assertEqual(app.rx_count, 1)
            self.assertEqual(app.info_vars['高程(m)'].get(), 'No Data')
        finally:
            app.on_close()


if __name__ == '__main__':
    unittest.main()
