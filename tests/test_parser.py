import unittest

from scraper.shopstar_bombas_millas import (
    extract_miles_values,
    extract_soles_values,
    parse_localized_number,
)


class ParserTests(unittest.TestCase):
    def test_soles_formats(self):
        self.assertEqual(parse_localized_number("1,299.90"), 1299.90)
        self.assertEqual(parse_localized_number("1.299,90"), 1299.90)
        self.assertEqual(extract_soles_values("Antes S/ 1,499.00 Ahora S/ 999.90"), [999.9, 1499.0])

    def test_miles_formats(self):
        self.assertEqual(extract_miles_values("Ahora 6,975 Millas Benefit"), [6975])
        self.assertEqual(extract_miles_values("Antes 10.000 millas, ahora 5.000 millas"), [5000, 10000])


if __name__ == "__main__":
    unittest.main()
