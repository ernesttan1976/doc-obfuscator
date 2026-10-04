import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getCandidateDecisionCounts,
  getCandidatesNotSelectedAtLevel,
  getVisibleCandidates,
} from './candidate-review.js';
import { findPageTermMatches, mapClientPointToLayer, mapClientRectToLayer } from './page-highlights.js';

const candidates = [
  { id: 'email', level: 2, decision: 'suggested' },
  { id: 'priority-five', level: 5, decision: 'suggested' },
  { id: 'date', level: 10, decision: 'suggested' },
  { id: 'priority-one', level: 1, decision: 'suggested' },
  { id: 'name', level: 4, decision: 'suggested' },
  { id: 'pinned-in', level: 8, decision: 'included' },
  { id: 'pinned-out', level: 10, decision: 'excluded' },
];

test('obfuscation level 1 selects none and higher levels include priorities up to the selected value', () => {
  assert.deepEqual(getVisibleCandidates(candidates, 1), []);
  assert.deepEqual(getVisibleCandidates(candidates, 5).map(({ id }) => id), [
    'email', 'priority-five', 'name',
  ]);
  assert.deepEqual(getVisibleCandidates(candidates, 10).map(({ id }) => id), [
    'email', 'priority-five', 'date', 'name', 'pinned-in', 'pinned-out',
  ]);
});

test('Include and Exclude decisions do not bypass the selected priority level', () => {
  assert.deepEqual(getVisibleCandidates(candidates, 8).map(({ id }) => id), [
    'email', 'priority-five', 'name', 'pinned-in',
  ]);
  assert.deepEqual(getVisibleCandidates(candidates, 1), []);
});

test('review counts cover all candidates and do not change with the selected level', () => {
  assert.deepEqual(getCandidateDecisionCounts(candidates), {
    suggested: 5,
    included: 1,
    excluded: 1,
  });
});

test('unselected candidates are counted outside the active priority range', () => {
  assert.equal(getCandidatesNotSelectedAtLevel(candidates, 1), candidates.length);
  assert.equal(getCandidatesNotSelectedAtLevel(candidates, 5), 4);
  assert.equal(getCandidatesNotSelectedAtLevel(candidates, 10), 1);
});

test('page term matching is case-insensitive and prefers the longest overlapping candidate', () => {
  const alex = { id: 'alex', term: 'Alex Tan' };
  const tan = { id: 'tan', term: 'Tan' };
  const cedar = { id: 'cedar', term: 'Project Cedar' };

  assert.deepEqual(
    findPageTermMatches('ALEX TAN met Alex Tan at Project Cedar.', [tan, alex, cedar])
      .map(({ start, end, candidate }) => ({ text: 'ALEX TAN met Alex Tan at Project Cedar.'.slice(start, end), id: candidate.id })),
    [
      { text: 'ALEX TAN', id: 'alex' },
      { text: 'Alex Tan', id: 'alex' },
      { text: 'Project Cedar', id: 'cedar' },
    ],
  );
});

test('page highlight geometry maps transformed client coordinates into overlay coordinates', () => {
  const bounds = { left: 100, top: 50, width: 200, height: 100 };
  const size = { width: 400, height: 200 };

  assert.deepEqual(
    mapClientRectToLayer({ left: 120, top: 60, width: 10, height: 5 }, bounds, size),
    { left: 40, top: 20, width: 20, height: 10 },
  );
  assert.deepEqual(mapClientPointToLayer(125, 65, bounds, size), { x: 50, y: 30 });
});
