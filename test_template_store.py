import unittest
from unittest import mock

import template_store as ts

REPLY = """Here is the classroom.

SAVE-TEMPLATE: scene | high school classroom
empty high school classroom, wide establishing view,
rows of student desks, chalkboard, wall clock, fluorescent ceiling lights
END-TEMPLATE

CONTEXT-LOG: drafted a classroom scene."""


class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status
    def json(self):
        return self._d
    def raise_for_status(self):
        if self.status_code >= 400:
            raise ts.requests.HTTPError(str(self.status_code))


class TemplateStoreTests(unittest.TestCase):
    def test_extract_strips_block_and_proposes_without_saving(self):
        with mock.patch.object(ts.requests, "post") as post:
            text, props = ts.extract_proposals(REPLY)
        post.assert_not_called()
        self.assertNotIn("SAVE-TEMPLATE", text)
        self.assertIn("CONTEXT-LOG", text)
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["key"], "high school classroom")
        self.assertEqual(props[0]["status"], "proposed")
        self.assertIn("rows of student desks", props[0]["text"])
        self.assertNotIn("\n", props[0]["text"])

    def test_no_block_returns_text_unchanged(self):
        text, props = ts.extract_proposals("just chatting")
        self.assertEqual((text, props), ("just chatting", []))

    def test_fenced_and_folded_header_body_is_cleaned(self):
        r = "SAVE-TEMPLATE: scene | gym\n```yaml\ngym: >\n  basketball court, bleachers, hoops, wooden floor\n```\nEND-TEMPLATE"
        _, props = ts.extract_proposals(r)
        self.assertEqual(props[0]["text"], "basketball court, bleachers, hoops, wooden floor")

    def _session(self):
        _, props = ts.extract_proposals(REPLY)
        return {"transcript": [{"role": "user", "text": "x"}, {"role": "assistant", "text": "y", "templates": props}]}

    def test_save_success_marks_saved_and_edits_win(self):
        s = self._session()
        with mock.patch.object(ts.requests, "post", side_effect=[FakeResp({"value": ""}), FakeResp({"saved": True})]) as post:
            p = ts.save_proposal(s, 1, 0, key="classroom", text="a classroom with many desks and a chalkboard")
        self.assertEqual(p["status"], "saved")
        add = post.call_args_list[1].kwargs["json"]
        self.assertEqual((add["action"], add["target"], add["key"]), ("add", "backgrounds", "classroom"))
        self.assertTrue(add["value"].startswith("a classroom"))
        with mock.patch.object(ts.requests, "post") as post:
            ts.save_proposal(s, 1, 0)           # already saved: no second write
        post.assert_not_called()

    def test_existing_key_not_overwritten(self):
        s = self._session()
        with mock.patch.object(ts.requests, "post", return_value=FakeResp({"value": "something"})) as post:
            p = ts.save_proposal(s, 1, 0)
        self.assertEqual(p["status"], "failed")
        self.assertIn("already exists", p["message"])
        self.assertEqual(post.call_count, 1)

    def test_unreachable_app_reports_failure(self):
        s = self._session()
        with mock.patch.object(ts.requests, "post", side_effect=ts.requests.ConnectionError("down")):
            p = ts.save_proposal(s, 1, 0)
        self.assertEqual(p["status"], "failed")
        self.assertIn("could not reach", p["message"])

    def test_validation_rejects_bad_name_and_unknown_kind(self):
        self.assertIn("YAML-special", ts.save_template({"kind": "scene", "key": "a: b", "text": "x" * 40})["message"])
        self.assertIn("unknown template kind", ts.save_template({"kind": "nope", "key": "a", "text": "x" * 40})["message"])

    def test_bad_index_raises(self):
        with self.assertRaises(LookupError):
            ts.save_proposal(self._session(), 0, 0)


if __name__ == "__main__":
    unittest.main()
