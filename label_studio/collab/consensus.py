"""Extensible agreement engine. Metrics are selected by annotation result type and can be registered/overridden."""

from itertools import combinations

BBOX_TYPES = {'rectanglelabels', 'rectangle'}
SPAN_TYPES = {'labels', 'hypertextlabels'}
CHOICE_TYPES = {'choices'}

METRICS = {}


def metric(*types):
    def register(fn):
        for t in types:
            METRICS[t] = fn
        return fn

    return register


def span_iou(a, b):
    start = max(a.get('start', 0), b.get('start', 0))
    end = min(a.get('end', 0), b.get('end', 0))
    inter = max(0, end - start)
    union = (a.get('end', 0) - a.get('start', 0)) + (b.get('end', 0) - b.get('start', 0)) - inter
    return inter / union if union > 0 else 0.0


def bbox_iou(a, b):
    ax2, ay2 = a.get('x', 0) + a.get('width', 0), a.get('y', 0) + a.get('height', 0)
    bx2, by2 = b.get('x', 0) + b.get('width', 0), b.get('y', 0) + b.get('height', 0)
    iw = max(0.0, min(ax2, bx2) - max(a.get('x', 0), b.get('x', 0)))
    ih = max(0.0, min(ay2, by2) - max(a.get('y', 0), b.get('y', 0)))
    inter = iw * ih
    union = a.get('width', 0) * a.get('height', 0) + b.get('width', 0) * b.get('height', 0) - inter
    return inter / union if union > 0 else 0.0


def _labels(value, key):
    return frozenset(value.get(key) or [])


def _match_regions(items_a, items_b, iou, key):
    """Greedy one-to-one matching of regions carrying the same labels; returns the sum of IoUs."""
    used, total = set(), 0.0
    for a in items_a:
        best, best_j = 0.0, None
        for j, b in enumerate(items_b):
            if j in used or _labels(a, key) != _labels(b, key):
                continue
            score = iou(a, b)
            if score > best:
                best, best_j = score, j
        if best_j is not None:
            used.add(best_j)
            total += best
    return total


def _region_score(items_a, items_b, iou, key):
    if not items_a and not items_b:
        return 1.0
    return _match_regions(items_a, items_b, iou, key) / max(len(items_a), len(items_b))


@metric(*CHOICE_TYPES)
def choices_metric(values_by_annotator):
    """Jaccard of the selected choices, plus per-label agreement (share of annotators agreeing on each label)."""
    sets = {k: set(c for v in vals for c in v.get('choices', [])) for k, vals in values_by_annotator.items()}
    pair_scores = []
    for (_, a), (_, b) in combinations(sets.items(), 2):
        pair_scores.append(len(a & b) / len(a | b) if (a | b) else 1.0)
    labels = set().union(*sets.values()) if sets else set()
    per_label = {}
    for label in sorted(labels):
        picks = sum(1 for s in sets.values() if label in s)
        per_label[label] = max(picks, len(sets) - picks) / len(sets)
    return pair_scores, per_label


@metric(*SPAN_TYPES)
def spans_metric(values_by_annotator):
    pair_scores = [
        _region_score(a, b, span_iou, 'labels')
        for (_, a), (_, b) in combinations(values_by_annotator.items(), 2)
    ]
    return pair_scores, {}


@metric(*BBOX_TYPES)
def bbox_metric(values_by_annotator):
    pair_scores = [
        _region_score(a, b, bbox_iou, 'rectanglelabels')
        for (_, a), (_, b) in combinations(values_by_annotator.items(), 2)
    ]
    return pair_scores, {}


def generic_metric(values_by_annotator):
    """Exact equality for result types without a dedicated metric."""
    pair_scores = [1.0 if a == b else 0.0 for (_, a), (_, b) in combinations(values_by_annotator.items(), 2)]
    return pair_scores, {}


def compute_agreement(results_by_annotator):
    """`results_by_annotator`: {annotator_key: [Label Studio result items]}.

    Returns {'score': float|None, 'by_control': {from_name: {...}}}. `score` is None with fewer than 2 annotators.
    """
    if len(results_by_annotator) < 2:
        return {'score': None, 'by_control': {}}
    controls = {}
    for key, items in results_by_annotator.items():
        for item in items or []:
            controls.setdefault((item.get('from_name'), item.get('type')), {}).setdefault(key, []).append(
                item.get('value', {})
            )
    by_control, scores = {}, []
    for (from_name, rtype), per_annotator in sorted(controls.items(), key=lambda x: str(x[0])):
        # annotators who produced nothing for this control still take part in the comparison
        values = {key: per_annotator.get(key, []) for key in results_by_annotator}
        fn = METRICS.get(rtype, generic_metric)
        pair_scores, per_label = fn(values)
        score = sum(pair_scores) / len(pair_scores) if pair_scores else None
        if score is not None:
            scores.append(score)
        by_control[from_name] = {'type': rtype, 'score': score, 'per_label': per_label}
    overall = sum(scores) / len(scores) if scores else 1.0
    return {'score': overall, 'by_control': by_control}
