export function getVisibleCandidates(candidates, level) {
  const visible = candidates.filter((candidate) => candidate.level >= 2 && candidate.level <= level);
  const scored = visible.filter((candidate) => Number.isInteger(candidate.reviewPriority));
  const unscored = visible.filter((candidate) => !Number.isInteger(candidate.reviewPriority));
  scored.sort((first, second) => second.reviewPriority - first.reviewPriority);
  return [...scored, ...unscored];
}

export function upsertCandidate(candidates, candidate) {
  const index = candidates.findIndex((item) => item.id === candidate.id);
  if (index < 0) return [...candidates, candidate];
  return candidates.map((item, itemIndex) => (
    itemIndex === index ? { ...item, ...candidate } : item
  ));
}

export function isCandidateAutoSuggested(candidate) {
  if (candidate.decision === 'excluded') return false;
  if (candidate.pinned) return candidate.decision === 'included';
  if (candidate.redactionConfidence != null) return true;
  return candidate.scoreStatus !== 'complete';
}

export function getCandidateDecisionCounts(candidates) {
  return candidates.reduce((counts, candidate) => {
    counts[candidate.decision] = (counts[candidate.decision] || 0) + 1;
    return counts;
  }, { suggested: 0, included: 0, excluded: 0 });
}

export function getCandidatesNotSelectedAtLevel(candidates, level) {
  return candidates.filter((candidate) => candidate.level < 2 || candidate.level > level).length;
}
