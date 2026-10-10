"""What the agent has seen so far, as records the model can query, supersede and re-read.

The runtime hands every observation it decides matters to `EvidenceStore.add`: a page or a result set as a
`page`, a downloaded file as a `document` (its raw bytes kept beside the record, reachable with `bytes_of`),
a selected mail as a `mail`, a resolved rule or approval as a `fact`. Kinds are open strings — the store never
restricts them, and it never copies scenario values: it holds what the model put in.

Two things the timed run needs from it: an audit trail (`invalidate`/`supersede` keep history, nothing is
deleted, so a wrong early reading stays visible next to the corrected one) and supersession lookup
(`find_latest` returns the newest active record no later than the scenario date for an exact reference, with
undated records matching but ranking below dated ones — the authority-and-time rule). `add` only *reports* a
conflict, it never resolves one: two active records that share a site and reference and disagree on revision
or date both stay active until the model calls `invalidate` or `supersede`.

Plain dicts and dict copies, so it is safe for interleaved use on a single asyncio loop: every mutation is
synchronous, no awaits inside them.
"""
from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime

__all__ = ["RECORD_FIELDS", "EvidenceStore"]

RECORD_FIELDS = ("rid", "kind", "site", "ref", "title", "url", "revision", "dated", "authority",
                 "text", "meta", "active", "created_at", "invalidated_by", "invalidation_reason")

# every add() keyword except kind, in declaration order: the fields the store keeps verbatim
_RECORDED = ("site", "ref", "title", "url", "revision", "dated", "authority", "text", "meta")
_DEFAULTS = dict.fromkeys(_RECORDED)
_CONFLICT_FIELDS = ("revision", "dated")
# after `dated` and creation time, a still-level tie breaks on rid (the contract's last resort), then on
# whatever else distinguishes the records, so a result is always deterministic
_RANK_TAIL = ("revision", "authority", "text")


class EvidenceStore:
    """Active/inactive records keyed by rid, plus the bytes of documents and conflict reporting."""

    def __init__(self) -> None:
        self._records: dict[str, dict] = {}
        self._order: list[str] = []
        self._bytes: dict[str, bytes] = {}

    # -- adding and reading -------------------------------------------------

    def add(self, kind: str, *, site: str | None = None, ref: str | None = None,
            title: str | None = None, url: str | None = None, revision: str | None = None,
            dated: str | None = None, authority: str | None = None, text: str | None = None,
            meta: dict | None = None, data: bytes | None = None) -> dict:
        """Store one observation and return it as a dict; the rid is assigned here.

        Keeps a separate copy of `meta`, and the raw `data` beside the record (`bytes_of`), so later caller
        changes cannot rewrite what the agent recorded. Reports a conflict — without resolving it — when an
        active record already shares this (site, ref) and disagrees on revision or dated.
        """
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError(f"kind must be a non-empty string, got {kind!r}")
        rid = uuid.uuid4().hex[:12]
        created_at = datetime.now(UTC).isoformat()
        record: dict = {"rid": rid, "kind": kind, **_DEFAULTS, "active": True, "created_at": created_at,
                        "invalidated_by": None, "invalidation_reason": None,
                        "site": site, "ref": ref, "title": title, "url": url, "revision": revision,
                        "dated": dated, "authority": authority, "text": text}
        record["meta"] = copy.deepcopy(meta) if isinstance(meta, dict) else None
        self._records[rid] = record
        self._order.append(rid)
        if data is not None:
            self._bytes[rid] = bytes(data)
        return self._copy(record)

    def get(self, rid: str) -> dict | None:
        """A copy of one record (bytes excluded — use `bytes_of`), or None for an unknown rid."""
        record = self._records.get(rid)
        return self._copy(record) if record else None

    def bytes_of(self, rid: str) -> bytes | None:
        """The original document bytes kept under `rid`, or None when nothing was stored."""
        payload = self._bytes.get(rid)
        return bytes(payload) if payload is not None else None

    def snapshot(self, *, kind: str | None = None, site: str | None = None, ref: str | None = None,
                 contains: str | None = None, active_only: bool = True,
                 limit: int | None = None) -> list[dict]:
        """Copies of the matching records, newest first, text included.

        `contains` is a case-insensitive substring of the record's text; `active_only=False` includes the
        superseded history. `limit` keeps the newest N.
        """
        needle = contains.casefold() if isinstance(contains, str) else None
        rows = [r for r in self._records.values()
                if (not active_only or r["active"])
                and (kind is None or r["kind"] == kind)
                and (site is None or r["site"] == site)
                and (ref is None or r["ref"] == ref)
                and (needle is None or needle in (r["text"] or "").casefold())]
        rows.sort(key=self._newest_key, reverse=True)
        if limit is not None:
            rows = rows[:max(0, limit)]
        return [self._copy(r) for r in rows]

    def find_latest(self, *, ref: str | None = None, authority: str | None = None,
                    site: str | None = None, kind: str | None = None,
                    not_after: str | None = None) -> dict | None:
        """The newest ACTIVE record matching every given filter, or None.

        `ref` matches exactly; `not_after` keeps records dated at or before it, and undated records match any
        `not_after` yet rank below every dated one — a dated record for the same reference wins the
        supersession lookup even when it was recorded later. Ties break on creation time, then rid.
        """
        candidates = [r for r in self._records.values()
                      if r["active"]
                      and (ref is None or r["ref"] == ref)
                      and (authority is None or r["authority"] == authority)
                      and (site is None or r["site"] == site)
                      and (kind is None or r["kind"] == kind)
                      and (not_after is None or r["dated"] is None or r["dated"] <= not_after)]
        if not candidates:
            return None
        return self._copy(max(candidates, key=self._latest_key))

    # -- resolving conflicts (the model decides; nothing is deleted) ---------

    def invalidate(self, rid: str, reason: str) -> bool:
        """Mark a record inactive, keeping it in the store; False for an unknown or already inactive rid."""
        record = self._records.get(rid)
        if record is None or not record["active"]:
            return False
        record["active"] = False
        record["invalidated_by"] = None
        record["invalidation_reason"] = reason
        return True

    def supersede(self, winner_rid: str, loser_rid: str, reason: str = "superseded") -> bool:
        """Mark the loser inactive and record what replaced it; False when either rid is unknown.

        A winner that was itself inactive is reactivated, so `supersede(a, b)` after `supersede(b, a)`
        restores the first record rather than leaving both inactive.
        """
        winner, loser = self._records.get(winner_rid), self._records.get(loser_rid)
        if winner is None or loser is None or winner_rid == loser_rid:
            return False
        winner["active"] = True
        winner["invalidated_by"] = None
        winner["invalidation_reason"] = None
        loser["active"] = False
        loser["invalidated_by"] = winner_rid
        loser["invalidation_reason"] = reason
        return True

    def conflicts(self) -> list[dict]:
        """ACTIVE records sharing (site, ref) that disagree on revision or dated: [{"a","b","why"}, ...]."""
        out: list[dict] = []
        active = [r for r in self._records.values() if r["active"]]
        for i, first in enumerate(active):
            for second in active[i + 1:]:
                if (first["site"], first["ref"]) != (second["site"], second["ref"]):
                    continue
                why = "; ".join(
                    f"{field} {first[field]!r} vs {second[field]!r}"
                    for field in _CONFLICT_FIELDS if first[field] != second[field])
                if why:
                    a, b = sorted((first["rid"], second["rid"]))
                    out.append({"a": a, "b": b, "why": why})
        return out

    def stats(self) -> dict:
        """Totals, active/inactive counts, counts by kind, conflicts and stored document bytes."""
        kinds: dict[str, int] = {}
        active = inactive = 0
        for record in self._records.values():
            kinds[record["kind"]] = kinds.get(record["kind"], 0) + 1
            if record["active"]:
                active += 1
            else:
                inactive += 1
        return {"total": len(self._records), "active": active, "inactive": inactive,
                "by_kind": kinds, "conflicts": len(self.conflicts()), "documents": len(self._bytes)}

    def export(self) -> dict:
        """The whole store, JSON-safe, for logs: records with text truncated, document bytes reduced to size."""
        documents = []
        for rid in self._order:
            if rid not in self._bytes:
                continue
            record = self._records[rid]
            documents.append({"rid": rid, "bytes": len(self._bytes[rid]), "kind": record["kind"]})
        return {"records": [self._export_record(r) for r in self._records.values()],
                "documents": documents,
                "conflicts": self.conflicts(),
                "stats": self.stats()}

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _copy(record: dict) -> dict:
        return copy.deepcopy(record)

    @staticmethod
    def _newest_key(record: dict) -> tuple:
        """Sort key for newest-first lists: full-precision creation time (ISO sorts as text), then rid."""
        return (str(record["created_at"] or ""), record["rid"])

    @staticmethod
    def _latest_key(record: dict) -> tuple:
        """Supersession ranking, compared with `max()`: dated above undated, then newest dated, then the
        full-precision creation time, then rid, then whatever else distinguishes the records.

        `dated` comes first, so a dated record outranks any undated one and any earlier-dated one; the
        microsecond clock only breaks records that are still level. Fields are coerced to strings so a mix
        of None and values on one key never raises.
        """
        tail = tuple(str(record[field] or "") for field in _RANK_TAIL)
        return (bool(record["dated"]), record["dated"] or "", str(record["created_at"] or ""), record["rid"],
                *tail)

    @staticmethod
    def _export_record(record: dict) -> dict:
        out = copy.deepcopy(record)
        text = out.get("text")
        if isinstance(text, str) and len(text) > 400:
            out["text"] = text[:400]
            out["text_truncated"] = True
        return out
