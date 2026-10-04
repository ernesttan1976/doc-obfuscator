import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getCandidateDecisionCounts,
  getSuggestedBelowSensitivityCount,
  getVisibleCandidates,
} from './candidate-review.js';

const candidates = [
  { id: 'email', level: 10, decision: 'suggested' },
  { id: 'date', level: 3, decision: 'suggested' },
  { id: 'name', level: 4, decision: 'suggested' },
  { id: 'pinned-in', level: 8, decision: 'included' },
  { id: 'pinned-out', level: 10, decision: 'excluded' },
];

test('sensitivity threshold reveals suggestions at or above the selected score', () => {
  assert.deepEqual(getVisibleCandidates(candidates, 1).map(({ id }) => id), [
    'email', 'date', 'name', 'pinned-in', 'pinned-out',
  ]);
  assert.deepEqual(getVisibleCandidates(candidates, 5).map(({ id }) => id), [
    'email', 'pinned-in', 'pinned-out',
  ]);
  assert.deepEqual(getVisibleCandidates(candidates, 10).map(({ id }) => id), [
    'email', 'pinned-in', 'pinned-out',
  ]);
});

test('manual Include and Exclude decisions remain visible at every threshold', () => {
  const visible = getVisibleCandidates(candidates, 10);
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

test('below-threshold count includes only hidden suggestions', () => {
  assert.equal(getSuggestedBelowSensitivityCount(candidates, 1), 0);
  assert.equal(getSuggestedBelowSensitivityCount(candidates, 5), 2);
});
