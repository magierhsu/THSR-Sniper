import unittest
from argparse import Namespace
from unittest.mock import MagicMock, patch
from bs4 import BeautifulSoup
from thsr_py.flows import _confirm_ticket_flow
from thsr_py.worker_protocol import WorkerResult, configure

class ConfirmationFlowTests(unittest.TestCase):
    def setUp(self):
        configure()
        self.args = Namespace(personal_id='A123456789', use_membership=False, _booking_result=WorkerResult())
        self.session = MagicMock()
        self.membership = patch('thsr_py.flows._process_membership', return_value=('0', {}))
        self.early = patch('thsr_py.flows._process_early_bird', return_value=None)
        self.membership.start()
        self.early.start()

    def tearDown(self):
        self.membership.stop()
        self.early.stop()

    def test_timeout_after_confirmation_is_uncertain_and_not_replayed(self):
        self.session.post.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            _confirm_ticket_flow(self.session, BeautifulSoup('', 'html.parser'), self.args)
        self.assertTrue(self.args._booking_result.uncertain)
        self.assertEqual(1, self.session.post.call_count)

    def test_unknown_confirmation_response_requires_review(self):
        self.session.post.return_value.text = '<html>unexpected</html>'
        _confirm_ticket_flow(self.session, BeautifulSoup('', 'html.parser'), self.args)
        self.assertTrue(self.args._booking_result.uncertain)

    def test_pnr_is_structured_even_without_complete_result_page(self):
        self.session.post.return_value.text = '<p class="pnr-code"><span>12345678</span></p>'
        _confirm_ticket_flow(self.session, BeautifulSoup('', 'html.parser'), self.args)
        self.assertEqual('12345678', self.args._booking_result.pnr)
        self.assertFalse(self.args._booking_result.uncertain)

    def test_definitive_rejection_allows_retry(self):
        self.session.post.return_value.text = '<span class="feedbackPanelERROR">已無座位</span>'
        _confirm_ticket_flow(self.session, BeautifulSoup('', 'html.parser'), self.args)
        self.assertFalse(self.args._booking_result.uncertain)

    def test_denied_persistence_permission_never_posts(self):
        with patch('thsr_py.flows.confirmation_permission', side_effect=RuntimeError('denied')):
            with self.assertRaises(RuntimeError):
                _confirm_ticket_flow(self.session, BeautifulSoup('', 'html.parser'), self.args)
        self.session.post.assert_not_called()
        self.assertFalse(self.args._booking_result.uncertain)
