import unittest
from competitive_client import api_cards, parse_competitive_payload


class CompetitiveClientTests(unittest.TestCase):
    def setUp(self):
        self.keys = [f"sc_{26000000+i}" for i in range(8)]
        self.deck = {"cards": self.keys, "source": "world_top_1000",
                     "provenance": {"sourceType": "world_top_1000", "sampleSize": 1000},
                     "stats": {"games": 100, "wins": 55, "losses": 40, "usageRate": 10, "bestWorldRank": 23}}

    def test_real_sample_draws_in_denominator(self):
        rows = parse_competitive_payload({"source": "world_top_1000", "decks": [self.deck]}, {})
        self.assertEqual((rows[0]["win_rate"], rows[0]["draws"], rows[0]["games"]), (55, 5, 100))
        self.assertEqual(rows[0]["best_world_rank"], 23)

    def test_legacy_and_invalid_data_rejected(self):
        for change in ({"source": "legacy"}, {"cards": self.keys[:7]}, {"cards": self.keys[:7]+self.keys[:1]},
                       {"stats": {"games": 1, "wins": 2, "losses": 0}}, {"provenance": {}}):
            rows = parse_competitive_payload({"source": "world_top_1000", "decks": [{**self.deck, **change}]}, {})
            self.assertEqual(rows, [])

    def test_unknown_official_and_selected_forms(self):
        forms = [{"key": self.keys[0], "form": "evolution"}, {"key": self.keys[1], "form": "hero"}]
        cards = api_cards(self.keys, {}, forms)
        self.assertEqual(len(cards), 8)
        self.assertEqual([c["evolutionLevel"] for c in cards[:3]], [1, 2, 0])


if __name__ == "__main__":
    unittest.main()
