import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getCandidateDecisionCounts,
  getSuggestedBelowLevelCount,
  getVisibleCandidates,
} from './candidate-review.js';

const candidates = [
  { id: 'email', level: 1, decision: 'suggested' },
  { id: 'date', level: 2, decision: 'suggested' },
  { id: 'name', level: 5, decision: 'suggested' },
  { id: 'pinned-in', level: 8, decision: 'included' },
  { id: 'pinned-out', level: 10, decision: 'excluded' },
];

test('candidate breadth reveals only suggested candidates up to the selected level', () => {
  assert.deepEqual(getVisibleCandidates(candidates, 1).map(({ id }) => id), [
    'email', 'pinned-in', 'pinned-out',
  ]);
  assert.deepEqual(getVisibleCandidates(candidates, 2).map(({ id }) => id), [
    'email', 'date', 'pinned-in', 'pinned-out',
  ]);
  assert.deepEqual(getVisibleCandidates(candidates, 5).map(({ id }) => id), [
    'email', 'date', 'name', 'pinned-in', 'pinned-out',
  ]);
});

test('manual Include and Exclude decisions remain visible at narrow breadth levels', () => {
  const visible = getVisibleCandidates(candidates, 1);
  assert.ok(visible.some(({ id }) => id === 'pinned-in'));
  assert.ok(visible.some(({ id }) => id === 'pinned-out'));
});

test('review counts cover all candidates and do not change with the selected level', () => {
  assert.deepEqual(getCandidateDecisionCounts(candidates), {
    suggested: 3,
    included: 1,
    excluded: 1,
  });
});

test('below-level count includes only hidden suggestions', () => {
  assert.equal(getSuggestedBelowLevelCount(candidates, 1), 2);
  assert.equal(getSuggestedBelowLevelCount(candidates, 5), 0);
});
