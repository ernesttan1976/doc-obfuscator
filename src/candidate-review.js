export function getVisibleCandidates(candidates, level) {
  return candidates.filter((candidate) => (
    candidate.level <= level || candidate.decision !== 'suggested'
  ));
}

export function getCandidateDecisionCounts(candidates) {
  return candidates.reduce((counts, candidate) => {
    counts[candidate.decision] = (counts[candidate.decision] || 0) + 1;
    return counts;
  }, { suggested: 0, included: 0, excluded: 0 });
}

export function getSuggestedBelowLevelCount(candidates, level) {
  return candidates.filter((candidate) => (
    candidate.level > level && candidate.decision === 'suggested'
  )).length;
}
