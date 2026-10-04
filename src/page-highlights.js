const escapeRegExp = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

export function findPageTermMatches(text, candidates) {
  const terms = candidates
    .filter((candidate) => typeof candidate.term === 'string' && candidate.term.length > 0)
    .sort((left, right) => right.term.length - left.term.length);
  if (!text || terms.length === 0) return [];

  const pattern = new RegExp(terms.map(({ term }) => escapeRegExp(term)).join('|'), 'giu');
  const matches = [];
  let match;
  while ((match = pattern.exec(text)) !== null) {
    const candidate = terms.find(({ term }) => term.toLowerCase() === match[0].toLowerCase());
    if (candidate) matches.push({ start: match.index, end: match.index + match[0].length, candidate });
  }
  return matches;
}
