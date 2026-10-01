import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from thsr_py import flows


class T0SessionFlowTests(unittest.TestCase):
    def test_formal_booking_always_creates_its_own_session(self):
        session = MagicMock()
        args = SimpleNamespace(
            opening_mode=True,
            pre_entry_seconds=60,
        )

        with patch('thsr_py.flows.create_booking_session', return_value=session) as create, \
             patch('thsr_py.flows._run_with_session') as run:
            flows.run(args)

        create.assert_called_once_with()
        run.assert_called_once_with(args, session)
        session.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
