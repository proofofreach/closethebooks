import unittest

from xero_absolute_backup.utils import flatten_record, slugify


class UtilsTests(unittest.TestCase):
    def test_flatten_record_handles_nested_values(self) -> None:
        flat = flatten_record({"InvoiceID": "1", "Contact": {"Name": "Acme"}, "LineItems": [{"x": 1}]})
        self.assertEqual(flat["InvoiceID"], "1")
        self.assertEqual(flat["Contact.Name"], "Acme")
        self.assertIn("LineItems", flat)

    def test_slugify(self) -> None:
        self.assertEqual(slugify("Example Org Ltd."), "example-org-ltd.")


if __name__ == "__main__":
    unittest.main()
