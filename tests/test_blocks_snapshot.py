"""catalog_blocks iterates a snapshot of the block table: a block record created while the catalogue is
being built (as ezdxf does for some dynamic/anonymous blocks) must not abort the whole drawing with
"OrderedDict mutated during iteration"."""
from collections import Counter

import ezdxf
from ezdxf import bbox as ezbbox

from aec_intelligence.operational import parsers


def test_catalog_blocks_tolerates_blocks_added_during_iteration(monkeypatch):
    doc = ezdxf.new()
    outer = doc.blocks.new("OUTER")  # iterated first, so later blocks are still pending when it mutates
    outer.add_blockref("INNER", (0, 0))
    inner = doc.blocks.new("INNER")
    inner.add_line((0, 0), (1, 1))
    real = parsers.effective_block_name
    created = []

    def creating_effective_name(entity):
        if not created:
            created.append(doc.blocks.new("*U77"))
        return real(entity)

    monkeypatch.setattr(parsers, "effective_block_name", creating_effective_name)
    result = {"objects": [{"id": "root"}], "metrics": Counter()}
    added = []
    sem = parsers._DXFSemantics("d", "t.dxf", {"source_hash": "h"}, doc, result, added.append, [], [])
    sem.catalog_blocks(ezbbox)
    assert created and {"INNER", "OUTER"} <= set(sem.blocks)
