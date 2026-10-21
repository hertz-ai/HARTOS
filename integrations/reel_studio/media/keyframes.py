"""integrations.reel_studio.media.keyframes -- which frames the VLM gets to judge.

The VLM scores at most MAX_KEYFRAMES frames per reel (plan WS-3: a 90 s
budget at about a second a frame).  ``pick_keyframes`` takes them from the
candidates the analysis pass kept (JPEG at a 768 px long side, so nothing
is decoded again):

1. each segment offers its best candidate (highest Quality.score; a tie
   goes to the earlier frame);
2. if that is k or fewer, all of them go;
3. otherwise the story's LAST segment (the last clip's last segment with a
   candidate) always goes, and the rest of the story (the clips end to end,
   in the order given, which is capture order, A1) is cut into k-1 equal
   slices of time, each giving its best offer; slices with none leave their
   place to the best offers not yet taken.

Under A1 the reel ends on the final-look reveal, which is filmed last.  A
short, softer final shot at the end of a slice full of crisp process shots
would lose its slice, the VLM would never see it, and final_look_found would
come back false; so the story's last segment is never left to the slices.
The other slices cover the transformation from bare face to final look
instead of piling up where the light happened to be best.  The result is in
story order and is deterministic: it depends on the candidates, not on the
order they arrive in.
"""
from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

from integrations.reel_studio.contracts import Candidate, ClipAnalysis, Keyframe

#: Keyframes per reel (plan section 7: the VLM budget).
MAX_KEYFRAMES = 25

#: (story time, clip index, candidate)
_Offer = Tuple[float, int, Candidate]


def _score(c: Candidate) -> float:
    return float(c.quality.score) if c.quality is not None else 0.0


def segment_bests(analysis: ClipAnalysis) -> List[Candidate]:
    """The best candidate of each segment that has one, in segment order."""
    best: Dict[int, Candidate] = {}
    for c in analysis.candidates:
        held = best.get(c.segment)
        if held is None or (-_score(c), c.t_s) < (-_score(held), held.t_s):
            best[c.segment] = c
    return [best[s] for s in sorted(best)]


def pick_keyframes(analyses: Sequence[ClipAnalysis], k: int = MAX_KEYFRAMES) -> List[Keyframe]:
    """At most ``k`` candidates, one per segment, spread over the story,
    always including the story's last segment (see the module note)."""
    if k <= 0:
        return []
    offers: List[_Offer] = []
    offset = 0.0
    for index, analysis in enumerate(analyses):
        offers.extend((offset + c.t_s, index, c) for c in segment_bests(analysis))
        offset += max(0.0, float(analysis.info.duration_s))
    if len(offers) <= k:
        return _story_order(offers)

    total = max(offset, max(o[0] for o in offers) + 1e-6)
    last = max(offers, key=lambda o: (o[1], o[2].segment))
    rest = [o for o in offers if o is not last]
    return _story_order(_spread(rest, k - 1, total) + [last])


def _spread(offers: List[_Offer], k: int, total: float) -> List[_Offer]:
    """The best offer of each of ``k`` equal slices of [0, total), topped up
    with the best offers left when slices are empty."""
    if k <= 0:
        return []
    slices: Dict[int, _Offer] = {}
    for offer in offers:
        s = min(k - 1, max(0, int(offer[0] / total * k)))
        if s not in slices or _rank(offer) < _rank(slices[s]):
            slices[s] = offer
    chosen = list(slices.values())
    if len(chosen) < k:
        taken = {id(o[2]) for o in chosen}
        spare = sorted((o for o in offers if id(o[2]) not in taken), key=_rank)
        chosen.extend(spare[:k - len(chosen)])
    return chosen


def _story_order(offers: List[_Offer]) -> List[Candidate]:
    return [c for _t, _i, c in sorted(offers, key=lambda o: (o[0], o[1]))]


def _rank(offer: _Offer) -> Tuple[float, float, int]:
    """Sort key, best first: higher score, then earlier in the story."""
    return (-_score(offer[2]), offer[0], offer[1])


__all__ = ['MAX_KEYFRAMES', 'segment_bests', 'pick_keyframes']
