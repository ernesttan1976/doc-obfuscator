import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getCandidateDecisionCounts,
  getCandidatesAtOrBelowPriority,
  getVisibleCandidates,
} from './candidate-review.js';

const candidates = [
  { id: 'email', level: 10, decision: 'suggested' },
  { id: 'priority-five', level: 5, decision: 'suggested' },
  { id: 'date', level: 3, decision: 'suggested' },
  { id: 'priority-one', level: 1, decision: 'suggested' },
  { id: 'name', level: 4, decision: 'suggested' },
  { id: 'pinned-in', level: 8, decision: 'included' },
  { id: 'pinned-out', level: 10, decision: 'excluded' },
];

test('priority cutoff excludes candidates at or below the selected value', () => {
  assert.deepEqual(getVisibleCandidates(candidates, 1).map(({ id }) => id), [
    'email', 'priority-five', 'date', 'name', 'pinned-in', 'pinned-out',
  ]);
  assert.deepEqual(getVisibleCandidates(candidates, 5).map(({ id }) => id), [
    'email', 'pinned-in', 'pinned-out',
  ]);
  assert.deepEqual(getVisibleCandidates(candidates, 10), []);
});

test('Include and Exclude decisions do not bypass the priority cutoff', () => {
  assert.deepEqual(getVisibleCandidates(candidates, 8).map(({ id }) => id), ['email', 'pinned-out']);
  assert.deepEqual(getVisibleCandidates(candidates, 10), []);
});

test('review counts cover all candidates and do not change with the selected level', () => {
  assert.deepEqual(getCandidateDecisionCounts(candidates), {
    suggested: 5,
    included: 1,
    excluded: 1,
  });
});

test('cutoff count includes every candidate at or below the selected priority', () => {
  assert.equal(getCandidatesAtOrBelowPriority(candidates, 1), 1);
  assert.equal(getCandidatesAtOrBelowPriority(candidates, 5), 4);
  assert.equal(getCandidatesAtOrBelowPriority(candidates, 10), candidates.length);
});
