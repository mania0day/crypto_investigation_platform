"""Assemble the attributor from every configured label source.

Ships with OFAC sanctions (vendored, license-verified). VASP labels are
supplied by the operator as labelpacks in ``labels/`` — CipherChain does not
ship invented exchange attributions, because a fabricated label is a
defamatory claim about a real address (vision §4: labels are sourced
claims, never guesses).
"""

from __future__ import annotations

import logging
from pathlib import Path

from cipherchain.analysis.attribution.labels import load_labelpack_dir
from cipherchain.analysis.attribution.store import LabelStoreAttributor
from cipherchain.analysis.sanctions.ofac import OfacSanctionsSource

logger = logging.getLogger(__name__)

# repo root / labels — alongside .env and client/, since labelpacks are
# operator-supplied data, not backend source.
# loader.py -> attribution -> analysis -> cipherchain -> src -> backend -> repo root
DEFAULT_LABELS_DIR = Path(__file__).resolve().parents[5] / "labels"


def build_attributor(
    labels_dir: Path | None = None, *, include_sanctions: bool = True
) -> LabelStoreAttributor:
    attributor = LabelStoreAttributor()
    if include_sanctions:
        attributor.add_source(OfacSanctionsSource())
    directory = labels_dir if labels_dir is not None else DEFAULT_LABELS_DIR
    for pack in load_labelpack_dir(directory):
        attributor.add_source(pack)
        logger.info("loaded labelpack %s (%d labels)", pack.name, len(pack.labels))
    logger.info(
        "attributor ready: %d addresses from %d source(s)",
        len(attributor),
        len(attributor.source_names),
    )
    return attributor
