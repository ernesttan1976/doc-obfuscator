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

export function mapClientRectToLayer(rect, layerBounds, layerSize) {
  const scaleX = layerBounds.width > 0 ? layerSize.width / layerBounds.width : 1;
  const scaleY = layerBounds.height > 0 ? layerSize.height / layerBounds.height : 1;
  return {
    left: (rect.left - layerBounds.left) * scaleX,
    top: (rect.top - layerBounds.top) * scaleY,
    width: rect.width * scaleX,
    height: rect.height * scaleY,
  };
}

export function mapClientPointToLayer(x, y, layerBounds, layerSize) {
  const scaleX = layerBounds.width > 0 ? layerSize.width / layerBounds.width : 1;
  const scaleY = layerBounds.height > 0 ? layerSize.height / layerBounds.height : 1;
  return {
    x: (x - layerBounds.left) * scaleX,
    y: (y - layerBounds.top) * scaleY,
  };
}
