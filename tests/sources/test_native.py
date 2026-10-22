"""Darwin boundary constants follow the installed SDK, not guessed selectors."""
import ctypes
import unittest
from unittest.mock import patch

from machine_audit.sources.native import Darwin, I32


class ProcessListTests(unittest.TestCase):
    def test_own_user_list_uses_uid_selector_not_process_group(self):
        # sys/proc_info.h: PROC_PGRP_ONLY=2, PROC_UID_ONLY=4.
        native = Darwin.__new__(Darwin)
        def listpids(kind, owner, buffer, size):
            self.assertEqual((kind, owner), (4, 501))
            if buffer is None:
                return 2 * ctypes.sizeof(I32)
            self.assertGreaterEqual(size, 2 * ctypes.sizeof(I32))
            buffer[0], buffer[1] = 101, 202
            return 2 * ctypes.sizeof(I32)
        native.listpids = listpids
        with patch('machine_audit.sources.native.os.getuid', return_value=501):
            self.assertEqual(native.processes(), (101, 202))


if __name__ == '__main__':
    unittest.main()
