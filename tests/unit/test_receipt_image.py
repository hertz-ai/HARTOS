"""Receipt PNG renderer + money math — legs A/B of the billing-receipt agent.

Money math is deterministic code (never the LLM): parse_amount and
compute_balance are pure and tested exhaustively.  Rendering tests skip
cleanly when Pillow is absent, mirroring og_image's optional-import stance.
"""
import os
import tempfile
import unittest

from integrations.channels.response.receipt_image import (
    compute_balance,
    parse_amount,
    render_receipt_png,
)

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


class ParseAmount(unittest.TestCase):

    def test_plain_number(self):
        self.assertEqual(parse_amount('5000'), 5000.0)

    def test_k_shorthand(self):
        self.assertEqual(parse_amount('18k'), 18000.0)

    def test_currency_symbol_and_commas(self):
        self.assertEqual(parse_amount('₹18,000'), 18000.0)

    def test_decimal(self):
        self.assertEqual(parse_amount('1250.50'), 1250.50)

    def test_junk_is_none(self):
        self.assertIsNone(parse_amount('call me maybe'))

    def test_empty_and_none(self):
        self.assertIsNone(parse_amount(''))
        self.assertIsNone(parse_amount(None))


class ComputeBalance(unittest.TestCase):

    def test_owner_example_18k_total_5k_advance(self):
        # "₹18k total, 5k advance" — the doc's canonical utterance.
        self.assertEqual(compute_balance('18k', '5k'), '13,000')

    def test_zero_advance(self):
        self.assertEqual(compute_balance('5000', '0'), '5,000')

    def test_decimal_balance_keeps_cents(self):
        self.assertEqual(compute_balance('100.75', '0.25'), '100.50')

    def test_unparseable_total_is_none(self):
        self.assertIsNone(compute_balance('a lot', '5000'))

    def test_unparseable_advance_is_none(self):
        self.assertIsNone(compute_balance('5000', 'some'))


@unittest.skipUnless(HAS_PIL, 'Pillow not installed')
class RenderReceiptPng(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='receipt_test_')
        self.fields = {
            'business_name': 'Glam by Meera',
            'client_name': 'Priya',
            'service': 'Bridal makeup',
            'amount': '18,000',
            'currency': 'INR',
            'date': '2026-09-12',
            'event_timing': 'ready by 6am',
            'advance': '5,000',
            'balance': '13,000',
            'notes': 'Includes trial session',
        }

    def test_renders_a_valid_png(self):
        path = render_receipt_png(self.fields, out_dir=self.tmp)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        with Image.open(path) as img:
            img.verify()  # raises on corrupt PNG
        with Image.open(path) as img:
            self.assertEqual(img.format, 'PNG')
            self.assertGreater(img.height, 400)

    def test_logo_is_composited(self):
        # A solid magenta logo: its pixels must appear in the rendered
        # receipt's logo region, which the no-logo render cannot contain.
        logo = os.path.join(self.tmp, 'logo.png')
        Image.new('RGB', (120, 120), (255, 0, 200)).save(logo)
        with_logo = render_receipt_png(self.fields, logo_path=logo,
                                       out_dir=self.tmp)
        with Image.open(with_logo) as img:
            px = img.convert('RGB')
            found = any(
                px.getpixel((x, y)) == (255, 0, 200)
                for x in range(300, 500, 20) for y in range(40, 220, 20)
            )
        self.assertTrue(found, 'magenta logo pixels absent from receipt')

    def test_unreadable_logo_does_not_break_render(self):
        bad = os.path.join(self.tmp, 'broken.png')
        with open(bad, 'w') as f:
            f.write('not an image')
        path = render_receipt_png(self.fields, logo_path=bad, out_dir=self.tmp)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))


if __name__ == '__main__':
    unittest.main()
