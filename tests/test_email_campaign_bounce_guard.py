"""The bounce guard must halt on an UNKNOWN rate, not just a high one.

Written after reading what actually happened to the nunba-gift campaign:

    sends    500 on 07-22, 1000 on 07-25, 1801 on 07-26
    bounces  recorded ONLY for 07-22
    07-22    264 post-send bounces out of 500 sent = 52.8%, ceiling is 5%

Bounce collection stopped after the first day, and 3,161 further messages went
out with no measurement at all. The guard did not stop them, and could not:
it reads the bounce log, so when bounce_handler.py stops writing, recent sends
carry zero bounces, the rate computes as 0/n = 0.0, and the ceiling test
passes. The campaign runs fastest exactly when nobody is watching it.

These tests drive send_campaign() and recent_bounce_rate() against real state
files on disk. They assert on BEHAVIOUR -- whether a batch is refused and how
many candidates survive -- rather than on the presence of any particular line
of source.
"""
import os
import sys
import time
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _day(offset_days):
    return time.strftime('%Y-%m-%d', time.localtime(time.time() + offset_days * 86400))


class BounceGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='campaign-state-')
        os.environ['HEVOLVE_EMAIL_STATE_DIR'] = self.tmp
        # Import AFTER the env var so the module picks up this state dir.
        for mod in [m for m in list(sys.modules) if 'email_campaign' in m]:
            del sys.modules[mod]
        from integrations.channels import email_campaign
        self.ec = email_campaign
        self.ec._STATE_DIR = self.tmp
        self.campaign = 'guardtest'

    def _write_sent(self, day, addresses):
        p = os.path.join(self.tmp, '%s.sent' % self.campaign)
        with open(p, 'a', encoding='utf-8') as f:
            for a in addresses:
                f.write('%s\t%s\n' % (day, a))

    def _write_bounced(self, addresses, mtime=None):
        p = os.path.join(self.tmp, 'bounced')
        with open(p, 'a', encoding='utf-8') as f:
            for a in addresses:
                f.write('%s\t5.1.1\tundelivered\t%s\n' % (_day(0), a))
        if mtime is not None:
            os.utime(p, (mtime, mtime))

    def test_fresh_log_with_no_bounces_is_not_stale(self):
        """A genuinely clean batch must still be allowed through."""
        addrs = ['u%d@example.com' % i for i in range(150)]
        self._write_sent(_day(0), addrs)
        # Bounce log exists and was written after the send: measured, and zero.
        self._write_bounced(['someone-else@example.com'], mtime=time.time() + 86400)

        r = self.ec.recent_bounce_rate(self.campaign)
        self.assertEqual(r['sent'], 150)
        self.assertEqual(r['bounced'], 0)
        self.assertFalse(r['stale'], 'a log written after the send is not stale')

    def test_missing_bounce_log_reads_as_stale_not_safe(self):
        """No bounce log at all means the rate is unknown, never 0%."""
        self._write_sent(_day(0), ['u%d@example.com' % i for i in range(150)])

        r = self.ec.recent_bounce_rate(self.campaign)
        self.assertEqual(r['rate'], 0.0, 'the arithmetic still yields zero')
        self.assertTrue(r['stale'], 'but zero here means unmeasured, not clean')

    def test_log_older_than_the_send_reads_as_stale(self):
        """The nunba-gift situation: sends continued, collection did not."""
        self._write_sent(_day(-3), ['old%d@example.com' % i for i in range(50)])
        self._write_bounced(['old1@example.com'], mtime=time.time() - 3 * 86400)
        # Two more days of sending after the bounce log went quiet.
        self._write_sent(_day(-1), ['new%d@example.com' % i for i in range(120)])

        r = self.ec.recent_bounce_rate(self.campaign)
        self.assertTrue(r['stale'],
                        'bounce log predates the newest send, so the rate is unknown')

    def test_send_campaign_refuses_a_batch_when_the_rate_is_unknown(self):
        """The behaviour that matters: nothing goes out on unmeasured data."""
        self._write_sent(_day(-1), ['u%d@example.com' % i for i in range(150)])
        # No bounce log written at all -> unknown.

        result = self.ec.send_campaign(
            recipients=['fresh%d@example.com' % i for i in range(10)],
            subject='x', html='<p>y</p>', text='y',
            campaign=self.campaign, dry_run=True,
        )
        self.assertIn('error', result)
        self.assertIn('stale', result['error'].lower())
        self.assertEqual(result['candidates'], 0,
                         'no candidates may survive an unknown bounce rate')

    def test_high_measured_rate_still_halts(self):
        """The original ceiling check must keep working."""
        addrs = ['u%d@example.com' % i for i in range(150)]
        self._write_sent(_day(0), addrs)
        # Fresh log (not stale) recording a rate far over the ceiling.
        self._write_bounced(addrs[:100], mtime=time.time() + 86400)

        result = self.ec.send_campaign(
            recipients=['fresh@example.com'],
            subject='x', html='<p>y</p>', text='y',
            campaign=self.campaign, dry_run=True,
        )
        self.assertIn('error', result)
        self.assertIn('ceiling', result['error'].lower())
        self.assertEqual(result['candidates'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
