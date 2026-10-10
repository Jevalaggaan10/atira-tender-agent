"""`EvidenceStore` against synthetic records: conflicts, supersession lookup, history, copies."""
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atira_agent.evidence import RECORD_FIELDS, EvidenceStore


def iso_offset(seconds: float) -> str:
    """A UTC ISO timestamp offset from now, to place records in time without patching the clock."""
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + seconds))


_KEEP = object()


def inject(store, rid, *, created_at=None, revision=_KEEP, dated=_KEEP, authority=_KEEP, text=_KEEP):
    """Force a stored record's ranking fields, so ties and ordering are deterministic in tests.

    Fields left out keep their stored value: pass `_KEEP` (the default) to touch only what a test needs.
    """
    record = store._records[rid]
    if created_at is not None:
        record["created_at"] = created_at
    for field, value in (("revision", revision), ("dated", dated), ("authority", authority), ("text", text)):
        if value is not _KEEP:
            record[field] = value
    return record


class AddAndConflict(unittest.TestCase):
    def test_record_has_every_field_and_rid_is_short_hex(self):
        store = EvidenceStore()
        record = store.add("mail", site="mail", ref="T-SYNTH-1", revision="2", dated="2026-01-02",
                           authority="finanzen", title="Bonus", url="https://mail.example/1",
                           text="hello", meta={"seen": True})
        self.assertEqual(sorted(record), sorted(RECORD_FIELDS))
        self.assertEqual(len(record["rid"]), 12)
        self.assertTrue(set(record["rid"]) <= set("0123456789abcdef"))
        self.assertTrue(record["active"])
        self.assertIsNone(record["invalidated_by"])
        self.assertIsNone(record["invalidation_reason"])
        self.assertEqual(record["created_at"][:10], iso_offset(0)[:10])

    def test_rids_unique_and_kinds_are_open(self):
        store = EvidenceStore()
        rids = {store.add(kind)["rid"] for kind in ("page", "document", "mail", "fact", "note", "Zä")}
        self.assertEqual(len(rids), 6)

    def test_add_rejects_an_empty_kind(self):
        for bad in ("", "   ", None, 7):
            with self.assertRaises(ValueError):
                EvidenceStore().add(bad)

    def test_conflict_when_active_records_share_site_ref_and_disagree(self):
        store = EvidenceStore()
        first = store.add("mail", site="mail", ref="T-SYNTH-1", revision="1", dated="2026-01-02")
        second = store.add("mail", site="mail", ref="T-SYNTH-1", revision="2", dated="2026-01-03")
        conflicts = store.conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual({conflicts[0]["a"], conflicts[0]["b"]}, {first["rid"], second["rid"]})
        self.assertIn("revision", conflicts[0]["why"])
        self.assertIn("dated", conflicts[0]["why"])
        self.assertTrue(store.get(first["rid"])["active"] and store.get(second["rid"])["active"])

    def test_no_conflict_for_identical_or_differently_keyed_records(self):
        store = EvidenceStore()
        store.add("mail", site="mail", ref="T-SYNTH-1")
        store.add("mail", site="mail", ref="T-SYNTH-1")                    # same key, same values
        store.add("mail", site="erp", ref="T-SYNTH-1")                     # different site
        store.add("mail", site="mail", ref="T-SYNTH-2")                    # different reference
        store.add("mail", site="mail", title="Kataloggruppe 12", text="a")  # both refs unset
        store.add("mail", site="mail", title="Kataloggruppe 12", text="b")
        self.assertEqual(store.conflicts(), [])

    def test_identical_adds_store_separate_records_without_conflict(self):
        store = EvidenceStore()
        fields = {"kind": "page", "site": "erp", "ref": "T-SYNTH-1", "revision": "3", "dated": "2026-01-05",
                    "text": "same"}
        first, second = store.add(**fields), store.add(**fields)
        self.assertNotEqual(first["rid"], second["rid"])
        self.assertEqual(store.conflicts(), [])
        self.assertEqual(store.stats()["active"], 2)

    def test_inactive_records_leave_the_conflict_list(self):
        store = EvidenceStore()
        old = store.add("mail", site="mail", ref="T-SYNTH-1", revision="1")
        new = store.add("mail", site="mail", ref="T-SYNTH-1", revision="2")
        self.assertEqual(len(store.conflicts()), 1)
        store.invalidate(old["rid"], "superseded by the newer revision")
        self.assertEqual(store.conflicts(), [])
        self.assertEqual(store.stats()["conflicts"], 0)
        self.assertTrue(store.get(new["rid"])["active"])


class CopySemantics(unittest.TestCase):
    def test_mutating_get_or_add_results_does_not_touch_the_store(self):
        store = EvidenceStore()
        returned = store.add("page", site="erp", ref="T-SYNTH-1", meta={"scales": [{"min": 1}]})
        returned["text"] = "edited"
        returned["meta"]["scales"][0]["min"] = 99
        returned["active"] = False
        stored = store.get(returned["rid"])
        self.assertIsNone(stored["text"])  # the store kept what add() recorded, not the edit
        self.assertEqual(stored["meta"], {"scales": [{"min": 1}]})
        self.assertTrue(stored["active"])

    def test_get_returns_a_fresh_copy_each_time(self):
        store = EvidenceStore()
        rid = store.add("fact", meta={"nested": {"deep": 1}})["rid"]
        first, second = store.get(rid), store.get(rid)
        self.assertIsNot(first, second)
        self.assertIsNot(first["meta"], second["meta"])
        self.assertIsNot(first["meta"]["nested"], second["meta"]["nested"])
        self.assertEqual(first, second)
        self.assertIsNone(store.get("0" * 12))

    def test_meta_and_bytes_are_copied_on_the_way_in_and_out(self):
        store = EvidenceStore()
        meta, payload = {"a": [1]}, bytearray(b"doc-bytes")
        rid = store.add("document", site="erp", meta=meta, data=payload)["rid"]
        meta["a"].append(2)
        payload[0:1] = b"X"
        self.assertEqual(store.get(rid)["meta"], {"a": [1]})
        self.assertEqual(store.bytes_of(rid), b"doc-bytes")
        again = store.bytes_of(rid)
        again += b"more"
        self.assertEqual(store.bytes_of(rid), b"doc-bytes")
        self.assertIsNone(store.bytes_of("0"*12))

    def test_snapshot_copies_and_meta_defaults_to_none(self):
        store = EvidenceStore()
        meta = {"k": "v"}
        store.add("page", site="erp", text="listing", meta=meta)
        meta["k"] = "changed"
        rows = store.snapshot()
        self.assertEqual(rows[0]["meta"], {"k": "v"})
        rows[0]["title"] = "edited"
        self.assertIsNone(store.get(rows[0]["rid"])["title"])
        self.assertIsNone(store.add("fact")["meta"])


class Snapshot(unittest.TestCase):
    def setUp(self):
        self.store = EvidenceStore()
        self.p1 = self.store.add("page", site="erp", text="alpha listing")["rid"]
        self.p2 = self.store.add("page", site="erp", text="Beta Listing")["rid"]
        self.doc = self.store.add("document", site="erp", ref="T-SYNTH-1", text="spec rev 4")["rid"]
        self.mail = self.store.add("mail", site="mail", text="Angebot")["rid"]
        for rid, offset in ((self.p1, -300), (self.p2, -200), (self.doc, -100), (self.mail, -50)):
            inject(self.store, rid, created_at=iso_offset(offset))

    def test_newest_first_and_limit_keep_the_newest(self):
        self.assertEqual([r["rid"] for r in self.store.snapshot()][:2], [self.mail, self.doc])
        self.assertEqual([r["rid"] for r in self.store.snapshot(limit=2)], [self.mail, self.doc])
        self.assertEqual(self.store.snapshot(limit=0), [])
        self.assertEqual(len(self.store.snapshot(limit=99)), 4)

    def test_filters(self):
        self.assertEqual([r["rid"] for r in self.store.snapshot(kind="page")], [self.p2, self.p1])
        self.assertEqual([r["rid"] for r in self.store.snapshot(site="mail")], [self.mail])
        self.assertEqual([r["rid"] for r in self.store.snapshot(ref="T-SYNTH-1")], [self.doc])

    def test_contains_is_case_insensitive_over_text(self):
        self.assertEqual([r["rid"] for r in self.store.snapshot(contains="ALPHA LISTING")], [self.p1])
        self.assertEqual([r["rid"] for r in self.store.snapshot(contains="Beta listing")], [self.p2])
        self.assertEqual([r["rid"] for r in self.store.snapshot(contains="listing")], [self.p2, self.p1])
        self.assertEqual(self.store.snapshot(contains="no-such-text"), [])
        self.assertEqual([r["rid"] for r in self.store.snapshot(contains="")],
                         [r["rid"] for r in self.store.snapshot()])  # blank text is inside any text

    def test_text_is_included_and_inactive_records_hidden_unless_asked(self):
        self.assertEqual(self.store.snapshot(kind="document")[0]["text"], "spec rev 4")
        self.assertIsNone(self.store.snapshot(kind="mail")[0]["revision"])  # unset fields stay present as None
        self.store.invalidate(self.p1, "wrong page")
        self.assertNotIn(self.p1, [r["rid"] for r in self.store.snapshot()])
        self.assertIn(self.p1, [r["rid"] for r in self.store.snapshot(active_only=False)])


class FindLatest(unittest.TestCase):
    def setUp(self):
        self.store = EvidenceStore()

    def test_exact_ref_match_only(self):
        hit = self.store.add("mail", ref="T-SYNTH-1", dated="2026-01-02")["rid"]
        self.store.add("mail", ref="T-SYNTH-1-EXTRA", dated="2026-01-03")
        self.store.add("mail", ref="T-OTHER", dated="2026-01-04")
        self.assertEqual(self.store.find_latest(ref="T-SYNTH-1")["rid"], hit)
        self.assertIsNone(self.store.find_latest(ref="T-SYNTH-1-EXTRA-X"))

    def test_other_filters(self):
        want = self.store.add("mail", ref="T-1", site="mail", authority="finanzen")["rid"]
        self.store.add("page", ref="T-1", site="mail", authority="finanzen")
        self.store.add("mail", ref="T-1", site="erp", authority="finanzen")
        self.store.add("mail", ref="T-1", site="mail", authority="technik")
        self.assertEqual(self.store.find_latest(ref="T-1", kind="mail", site="mail",
                                               authority="finanzen")["rid"], want)
        self.assertIsNone(self.store.find_latest(ref="T-1", kind="document"))

    def test_two_records_on_the_same_date_resolve_by_creation_time(self):
        """Back-to-back inserts of one date: the one stored later wins, even within the same second."""
        earlier = self.store.add("fact", ref="T-1", dated="2026-01-02", text="earlier note")
        later = self.store.add("fact", ref="T-1", dated="2026-01-02", text="later note")["rid"]
        stamp = self.store.get(earlier["rid"])["created_at"][:19]
        self.assertEqual(self.store.find_latest(ref="T-1", kind="fact")["rid"], later)
        # same date, same clock: the greater rid decides
        inject(self.store, earlier["rid"], created_at=stamp + "+00:00")
        inject(self.store, later, created_at=stamp + "+00:00")
        self.assertEqual(self.store.find_latest(ref="T-1")["rid"], max(earlier["rid"], later))

    def test_dated_wins_over_undated_whatever_the_order(self):
        undated = self.store.add("fact", ref="T-1", text="undated rule")["rid"]
        dated = self.store.add("fact", ref="T-1", dated="2026-01-01")["rid"]
        self.assertEqual(self.store.find_latest(ref="T-1")["rid"], dated)
        store = EvidenceStore()  # dated added first, the undated one recorded later
        early = store.add("fact", ref="T-1", dated="2026-01-01")["rid"]
        late_undated = store.add("fact", ref="T-1", text="undated rule")["rid"]
        self.assertEqual(store.find_latest(ref="T-1")["rid"], early)
        # past the dated record's day, the undated one is all that still applies
        self.assertEqual(store.find_latest(ref="T-1", not_after="2025-12-31")["rid"], late_undated)
        self.assertEqual(store.find_latest(ref="T-1", not_after="2026-01-01")["rid"], early)
        self.assertIsNotNone(undated)

    def test_not_after_boundary_is_inclusive(self):
        inside = self.store.add("fact", ref="T-1", dated="2026-03-15")["rid"]
        outside = self.store.add("fact", ref="T-1", dated="2026-03-16")["rid"]
        self.assertEqual(self.store.find_latest(ref="T-1", not_after="2026-03-15")["rid"], inside)
        self.assertEqual(self.store.find_latest(ref="T-1", not_after="2026-03-16")["rid"], outside)
        self.assertIsNone(self.store.find_latest(ref="T-1", not_after="2026-03-14"))
        self.assertIsNone(self.store.find_latest(ref="T-1", not_after="2026-01-01"))

    def test_newest_dated_wins_and_undated_only_when_nothing_dated_applies(self):
        old = self.store.add("fact", ref="T-1", dated="2026-01-31")["rid"]
        newer = self.store.add("fact", ref="T-1", dated="2026-02-28")["rid"]
        self.assertEqual(self.store.find_latest(ref="T-1", not_after="2026-02-01")["rid"], old)
        self.assertEqual(self.store.find_latest(ref="T-1", not_after="2026-03-01")["rid"], newer)

    def test_dated_authority_survives_undated_noise(self):
        """Two undated notes never displace the dated record; they only order among themselves."""
        dated = self.store.add("fact", ref="T-1", dated="2026-01-02")["rid"]
        first_undated = self.store.add("fact", ref="T-1", text="first")["rid"]
        second_undated = self.store.add("fact", ref="T-1", text="second")["rid"]
        self.assertEqual(self.store.find_latest(ref="T-1")["rid"], dated)
        self.store.invalidate(dated, "date was wrong after all")
        self.assertEqual(self.store.find_latest(ref="T-1")["rid"], second_undated)
        self.store.invalidate(second_undated, "duplicate of the same note")
        self.assertEqual(self.store.find_latest(ref="T-1")["rid"], first_undated)
        self.store.invalidate(first_undated, "wrong note")
        self.assertIsNone(self.store.find_latest(ref="T-1"))
        # the dated record comes back and immediately outranks the undated ones again
        self.assertTrue(self.store.supersede(dated, first_undated))
        self.assertEqual(self.store.find_latest(ref="T-1")["rid"], dated)

    def test_ties_break_on_created_at_then_rid(self):
        first = self.store.add("fact", ref="T-1", dated="2026-01-02", revision="1")["rid"]
        second = self.store.add("fact", ref="T-1", dated="2026-01-02", revision="1")["rid"]
        earlier, later = sorted((first, second))  # rids are random, so pin which is which
        inject(self.store, earlier, created_at=iso_offset(-600), dated="2026-01-02", revision="1")
        inject(self.store, later, created_at=iso_offset(-10), dated="2026-01-02", revision="1")
        self.assertEqual(self.store.find_latest(ref="T-1")["rid"], later)
        # identical timestamps, and a blank clock: ranking falls through to the greater rid
        for stamp in (iso_offset(-300), ""):
            inject(self.store, earlier, created_at=stamp, dated="2026-01-02", revision="1")
            inject(self.store, later, created_at=stamp, dated="2026-01-02", revision="1")
            self.assertEqual(self.store.find_latest(ref="T-1")["rid"], max(earlier, later))

    def test_inactive_records_never_win(self):
        winner = self.store.add("fact", ref="T-1", dated="2026-01-01")["rid"]
        stale = self.store.add("fact", ref="T-1", dated="2026-05-05")["rid"]
        self.store.invalidate(stale, "future-dated record does not apply")
        self.assertEqual(self.store.find_latest(ref="T-1")["rid"], winner)

    def test_empty_result_is_none(self):
        self.assertIsNone(self.store.find_latest())
        self.assertIsNone(self.store.find_latest(ref="T-NONE"))


class History(unittest.TestCase):
    def setUp(self):
        self.store = EvidenceStore()
        self.old = self.store.add("mail", site="mail", ref="T-SYNTH-1", revision="1", dated="2026-01-02")["rid"]
        self.new = self.store.add("mail", site="mail", ref="T-SYNTH-1", revision="2", dated="2026-01-03")["rid"]

    def test_invalidate_marks_inactive_and_keeps_the_record(self):
        self.assertTrue(self.store.invalidate(self.old, "withdrawn by a later answer"))
        record = self.store.get(self.old)
        self.assertFalse(record["active"])
        self.assertIsNone(record["invalidated_by"])
        self.assertEqual(record["invalidation_reason"], "withdrawn by a later answer")
        self.assertIn(self.old, [r["rid"] for r in self.store.snapshot(active_only=False)])
        self.assertEqual(self.store.stats()["inactive"], 1)

    def test_invalidate_is_idempotent_and_false_for_unknown(self):
        self.store.invalidate(self.old, "first reason")
        self.assertFalse(self.store.invalidate(self.old, "second reason"))
        self.assertEqual(self.store.get(self.old)["invalidation_reason"], "first reason")
        self.assertFalse(self.store.invalidate("0" * 12, "nothing here"))

    def test_supersede_links_the_loser_to_the_winner(self):
        self.assertTrue(self.store.supersede(self.new, self.old))
        loser = self.store.get(self.old)
        self.assertFalse(loser["active"])
        self.assertEqual(loser["invalidated_by"], self.new)
        self.assertEqual(loser["invalidation_reason"], "superseded")
        self.assertTrue(self.store.get(self.new)["active"])
        self.assertEqual(self.store.conflicts(), [])

    def test_supersede_with_a_reason_reactivates_a_stale_winner(self):
        self.assertTrue(self.store.invalidate(self.new, "misread the header"))
        self.assertTrue(self.store.supersede(self.new, self.old, reason="the header was right after all"))
        self.assertTrue(self.store.get(self.new)["active"])
        self.assertIsNone(self.store.get(self.new)["invalidation_reason"])
        self.assertEqual(self.store.get(self.old)["invalidation_reason"], "the header was right after all")

    def test_supersede_refuses_unknown_or_identical_rids(self):
        self.assertFalse(self.store.supersede(self.new, "0" * 12))
        self.assertFalse(self.store.supersede("0" * 12, self.new))
        self.assertFalse(self.store.supersede(self.new, self.new))
        self.assertTrue(self.store.get(self.new)["active"])
        self.assertTrue(self.store.get(self.old)["active"])

    def test_resolving_a_conflict_leaves_the_other_records_alone(self):
        unrelated_old = self.store.add("mail", site="mail", ref="T-SYNTH-2", revision="1")["rid"]
        unrelated_new = self.store.add("mail", site="mail", ref="T-SYNTH-2", revision="2")["rid"]
        self.store.supersede(self.new, self.old)
        conflicts = self.store.conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual({conflicts[0]["a"], conflicts[0]["b"]}, {unrelated_old, unrelated_new})
        self.assertTrue(self.store.get(unrelated_new)["active"])


class StatsAndExport(unittest.TestCase):
    def build(self):
        store = EvidenceStore()
        store.add("page", site="erp", ref="T-SYNTH-1", revision="1", text="first spec")
        document = store.add("document", site="erp", ref="T-SYNTH-1", revision="2", text="second spec",
                             data=b"fake-pdf-bytes")
        store.add("mail", site="mail", text="Angebot über_catalog")
        store.invalidate(store.snapshot(kind="page")[0]["rid"], "wrong revision")
        return store, document

    def test_stats_counts_kinds_active_inactive_and_conflicts(self):
        store, _ = self.build()
        self.assertEqual(store.stats()["total"], 3)
        self.assertEqual(store.stats()["documents"], 1)
        # the only pair that shared (site, ref) disagreed on revision, and one of them is now inactive
        self.assertEqual(store.stats()["conflicts"], 0)
        self.assertEqual(store.stats()["by_kind"], {"page": 1, "document": 1, "mail": 1})
        store.add("mail", site="mail", ref="T-SYNTH-3", revision="1")
        store.add("mail", site="mail", ref="T-SYNTH-3", revision="2")
        self.assertEqual(store.stats(), {"total": 5, "active": 4, "inactive": 1,
                                        "by_kind": {"page": 1, "document": 1, "mail": 3},
                                        "conflicts": 1, "documents": 1})
        empty = EvidenceStore()
        self.assertEqual(empty.stats(), {"total": 0, "active": 0, "inactive": 0, "by_kind": {},
                                        "conflicts": 0, "documents": 0})

    def test_export_is_json_dumpable_and_holds_no_bytes(self):
        store, document = self.build()
        other = store.add("mail", site="mail", ref="T-SYNTH-4", revision="1")
        store.add("mail", site="mail", ref="T-SYNTH-4", revision="2")
        payload = json.dumps(store.export())
        out = json.loads(payload)
        self.assertEqual(sorted(out), ["conflicts", "documents", "records", "stats"])
        self.assertEqual(len(out["records"]), 5)
        self.assertEqual(out["documents"], [{"rid": document["rid"], "bytes": 14, "kind": "document"}])
        for record in out["records"]:
            self.assertEqual(sorted(record), sorted(RECORD_FIELDS))
            self.assertNotIn("data", record)
            self.assertNotIn("bytes", record)
        self.assertEqual(len(out["conflicts"]), 1)
        self.assertEqual({other["rid"]} & {out["conflicts"][0]["a"], out["conflicts"][0]["b"]}, {other["rid"]})
        self.assertEqual(out["stats"], store.stats())

    def test_export_truncates_long_text_and_deep_copies(self):
        store = EvidenceStore()
        meta = {"nested": {"deep": 1}}
        store.add("fact", text="x" * 900, meta=meta)
        out = store.export()
        record = out["records"][0]
        self.assertEqual(len(record["text"]), 400)
        self.assertTrue(record["text_truncated"])
        record["meta"]["nested"]["deep"] = 99
        self.assertEqual(store.get(record["rid"])["meta"], {"nested": {"deep": 1}})
        short = store.add("fact", text="short")["rid"]
        by_rid = {r["rid"]: r for r in store.export()["records"]}
        self.assertEqual(by_rid[short]["text"], "short")
        self.assertNotIn("text_truncated", by_rid[short])


class InterleavedAsyncUse(unittest.IsolatedAsyncioTestCase):
    async def test_adds_and_mutations_survive_interleaved_tasks(self):
        store = EvidenceStore()

        async def add_many(kind, count, offset):
            rids = []
            for index in range(count):
                rids.append(store.add(kind, site="erp", ref=f"T-SYNTH-{offset + index}",
                                      revision="1", text=f"{kind} {index}")["rid"])
                await asyncio.sleep(0)  # hand the loop to the other task
            return rids

        async def churn():
            for index in range(10):
                await asyncio.sleep(0)
                for row in store.snapshot(limit=3):
                    store.get(row["rid"])
                    store.conflicts()
            return True

        (first, second), keep_busy = await asyncio.gather(
            asyncio.gather(add_many("page", 25, 0), add_many("mail", 25, 0)), churn())
        self.assertTrue(keep_busy)
        self.assertEqual(len({*first, *second}), 50)
        self.assertEqual(store.stats()["total"], 50)
        self.assertEqual(store.stats()["conflicts"], 0)
        for rid in first[:5]:
            self.assertTrue(store.invalidate(rid, "cleanup"))
        self.assertEqual(store.stats()["active"], 45)


if __name__ == "__main__":
    unittest.main()
