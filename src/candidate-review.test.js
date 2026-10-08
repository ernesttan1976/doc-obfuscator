import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getCandidateDecisionCounts,
  getCandidatesMatchingSignal,
  getCandidatesNotSelectedAtLevel,
  getIncludedCandidates,
  getReviewCandidates,
  getVisibleCandidates,
  isCandidateAutoSuggested,
  upsertCandidate,
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

test('automatically excluded candidates are omitted from review while pinned exclusions remain manageable', () => {
  const review = getReviewCandidates([
    { id: 'auto-excluded', decision: 'excluded', pinned: false },
    { id: 'manual-excluded', decision: 'excluded', pinned: true },
    { id: 'suggested', decision: 'suggested' },
    { id: 'queued', decision: 'suggested', scoreStatus: 'queued' },
  ]);
  assert.deepEqual(review.map(({ id }) => id), ['manual-excluded', 'suggested']);
});

test('the frontend included-candidate list omits suggestions, exclusions, and queued candidates', () => {
  const included = getIncludedCandidates([
    { id: 'included', decision: 'included' },
    { id: 'suggested', decision: 'suggested' },
    { id: 'excluded', decision: 'excluded', pinned: true },
    { id: 'queued-included', decision: 'included', scoreStatus: 'queued' },
  ]);

  assert.deepEqual(included.map(({ id }) => id), ['included']);
});

test('queued Stage 1 words stay out of review and preview until scoring completes', () => {
  const queued = { id: 'queued', level: 8, decision: 'suggested', scoreStatus: 'queued' };
  const commonWord = {
    id: 'common-word',
    level: 8,
    decision: 'excluded',
    scoreStatus: 'complete',
    signals: {
      isCommonWord: { answer: 'Yes' },
      isIdentifier: { answer: 'No' },
      isOperationallySignificant: { answer: 'No' },
    },
  };
  const scoredCandidate = { id: 'scored', level: 8, decision: 'suggested', scoreStatus: 'complete' };

  assert.deepEqual(getReviewCandidates([queued, commonWord, scoredCandidate]).map(({ id }) => id), ['scored']);
  assert.deepEqual(getVisibleCandidates([queued, commonWord, scoredCandidate], 10).map(({ id }) => id), [
    'common-word', 'scored',
  ]);
});

test('candidates with Ollaya review priorities are ordered highest review priority first', () => {
  const scored = [
    { id: 'priority-three', level: 2, reviewPriority: 3 },
    { id: 'priority-ten', level: 8, reviewPriority: 10 },
    { id: 'priority-six', level: 5, reviewPriority: 6 },
    { id: 'unscored', level: 4 },
  ];
  assert.deepEqual(getVisibleCandidates(scored, 10).map(({ id }) => id), [
    'priority-ten', 'priority-six', 'priority-three', 'unscored',
  ]);
});

test('streamed candidates are appended as discovered and later updates preserve existing fields', () => {
  const first = { id: 'first', term: 'Alex Tan', decision: 'suggested' };
  const second = { id: 'second', term: 'alex@example.test' };
  const initial = [first];
  const appended = upsertCandidate(initial, second);
  const updated = upsertCandidate(appended, { ...first, category: 'NER_ENTITY' });

  assert.deepEqual(appended, [first, second]);
  assert.deepEqual(updated, [{ ...first, category: 'NER_ENTITY' }, second]);
  assert.deepEqual(initial, [first]);
});

test('unscored word tokens stay review-only until Ollaya affirms a sensitive signal', () => {
  assert.equal(isCandidateAutoSuggested({ category: 'WORD', scoreStatus: 'unavailable', level: 8 }), false);
  assert.equal(isCandidateAutoSuggested({ category: 'WORD', scoreStatus: 'complete', level: 8 }), false);
  assert.equal(isCandidateAutoSuggested({ category: 'WORD', redactionConfidence: 0.8, level: 4 }), true);
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

test('signal selection requires an affirmative answer and meets the requested confidence threshold', () => {
  const scored = [
    { id: 'strong-identifier', signals: { isIdentifier: { answer: 'Yes', probabilityYes: 0.91 } } },
    { id: 'weak-identifier', signals: { isIdentifier: { answer: 'Yes', probabilityYes: 0.72 } } },
    { id: 'negative', signals: { isIdentifier: { answer: 'No', probabilityYes: 0.18 } } },
    { id: 'missing', signals: {} },
  ];

  assert.deepEqual(getCandidatesMatchingSignal(scored, 'isIdentifier', 80).map(({ id }) => id), [
    'strong-identifier',
  ]);
  assert.deepEqual(getCandidatesMatchingSignal(scored, 'isIdentifier', 50).map(({ id }) => id), [
    'strong-identifier', 'weak-identifier',
  ]);
  assert.deepEqual(getCandidatesMatchingSignal(scored, 'isIdentifier', 101), []);
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
