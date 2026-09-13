import unittest
from deck_inference import extract_player_decks, normalized_name
def cards(n=8, start=26000500):
    return [{"id": start+i, "name": f"New {i}"} for i in range(n)]
def battle(n=8):
    return {"type":"pathOfLegend", "battleTime":"20260913T100000.000Z",
            "team":[{"tag":"#P0Y","cards":cards(n),"crowns":2}],
            "opponent":[{"tag":"#Q2L","cards":cards(start=26000600),"crowns":1}]}
class DeckTests(unittest.TestCase):
    def test_team(self):
        self.assertEqual(extract_player_decks([battle()], " #p0y ")[0]["cards"][0]["id"], 26000500)
    def test_opponent(self):
        b=battle(); b["team"],b["opponent"]=b["opponent"],b["team"]
        row=extract_player_decks([b],"P0Y")[0]
        self.assertEqual(row["cards"][0]["id"],26000500)
        self.assertEqual(row["result"],"win")
    def test_skip_invalid(self):
        for n in (7,16,24):
            self.assertEqual(len(extract_player_decks([battle(n),battle()],"P0Y")),1)
    def test_duplicate(self):
        b=battle();b["team"][0]["cards"][7]=b["team"][0]["cards"][0]
        self.assertEqual(extract_player_decks([b],"P0Y"),[])
    def test_duels(self):
        for kind in ("riverRaceDuel","friendlyDuel"):
            b=battle();b["type"]=kind
            self.assertEqual(extract_player_decks([b],"P0Y"),[])
    def test_exact_tag(self):
        self.assertEqual(extract_player_decks([battle()],"P0"),[])
    def test_name(self):
        self.assertEqual(normalized_name(" ＭＩＮＩ. P.E.K.K.A! "), "mini p e k k a")
if __name__ == "__main__":
    unittest.main()
