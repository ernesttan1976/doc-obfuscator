import { useEffect, useRef, useState } from 'react';
import pdfWorker from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { getCandidateDecisionCounts, getCandidatesMatchingSignal, getCandidatesNotSelectedAtLevel, getIncludedCandidates, getReviewCandidates, getVisibleCandidates, isCandidateAutoSuggested, upsertCandidate } from './candidate-review.js';
import { findPageTermMatches, mapClientPointToLayer, mapClientRectToLayer } from './page-highlights.js';
import './stage3-preview.css';

const sampleText = [
  'Alex Tan will brief the board on Project Cedar at the Tuesday session. The working team has moved the launch window to the second week of November.',
  'The current owner is A. Tan. Please keep the Cedar reference out of external notes until the partner review is complete. Finance has approved the revised operating envelope.',
];

const groups = [
  {
    id: 'alex',
    term: 'Alex Tan',
    members: ['Alex Tan', 'A. Tan'],
    confidence: '96%',
    reason: 'Example manually confirmed group',
    token: 'T_001',
    level: 2,
    confirmed: true,
  },
];

const initialFiles = [
  { name: 'board-update.docx', type: 'DOCX', status: 'Needs review · v03', content: sampleText, heading: 'Project Cedar: Q4 operating brief' },
  { name: 'launch-brief.pptx', type: 'PPTX', status: 'Ready to send', content: ['Alex Tan will share the Project Cedar launch window with the partner team.', 'Cedar milestones remain internal until the review is complete.'], heading: 'Launch briefing' },
  { name: 'notes.md', type: 'MD', status: 'Original', content: ['Project Cedar notes', 'Owner: Alex Tan (A. Tan). Keep the Cedar reference private until sign-off.'], heading: 'Working notes' },
  { name: 'agent-return.pptx', type: 'PPTX', status: 'Restore available', content: ['Project owner: [[T_001]]', 'Project: [[T_002]]'], heading: 'Returned presentation' },
];

const RECENT_PROJECTS_KEY = 'blot-recent-projects';
const ACTIVE_PROJECT_KEY = 'blot-active-project';
const ACTIVE_DOCUMENT_KEY = 'blot-active-document';
const OLLAYA_CSV_COLUMNS = [
  ['word', 'Word'],
  ['is_identifier_percent', 'Identifier'],
  ['is_operationally_significant_percent', 'Operational'],
  ['is_common_word_percent', 'Common word'],
  ['is_correct', 'Correct'],
];

const csvRowFromCandidate = (candidate) => ({
  word: candidate.term,
  is_identifier_percent: candidate.signals?.isIdentifier?.probabilityYes ?? '',
  is_operationally_significant_percent: candidate.signals?.isOperationallySignificant?.probabilityYes ?? '',
  is_common_word_percent: candidate.signals?.isCommonWord?.probabilityYes ?? '',
  is_correct: 'N',
});

const readRecentProjects = () => {
  try {
    const projects = JSON.parse(localStorage.getItem(RECENT_PROJECTS_KEY) || '[]');
    return Array.isArray(projects)
      ? projects.filter((project) => typeof project?.name === 'string' && typeof project?.directory === 'string')
      : [];
  } catch {
    return [];
  }
};

const readActiveDocumentId = () => {
  try {
    return localStorage.getItem(ACTIVE_DOCUMENT_KEY) || '';
  } catch {
    return '';
  }
};

const rememberActiveDocument = (documentId) => {
  if (!documentId) return;
  try {
    localStorage.setItem(ACTIVE_DOCUMENT_KEY, documentId);
  } catch { /* Restoring the selected document is optional when browser storage is unavailable. */ }
};

const countMatches = (text, value) => {
  const escaped = value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return (text.match(new RegExp(escaped, 'gi')) || []).length;
};

const countGroupMatches = (text, members) => {
  const ranges = members.flatMap((member) => {
    const escaped = member.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return [...text.matchAll(new RegExp(escaped, 'gi'))].map((match) => [match.index, match.index + match[0].length]);
  }).sort((a, b) => a[0] - b[0] || b[1] - a[1]);
  let count = 0;
  let end = -1;
  for (const [start, nextEnd] of ranges) {
    if (start >= end) count += 1;
    end = Math.max(end, nextEnd);
  }
  return count;
};

const previewLines = (preview) => {
  const content = typeof preview.text === 'string'
    ? preview.text.split(/\r\n|\r|\n/)
    : preview.format === 'CSV'
      ? preview.rows.map((row, index) => `${index + 1}  ${row.map((cell) => cell.text).join('  │  ')}`)
      : preview.sheets.flatMap((sheet) => [sheet.name, ...sheet.cells.map((cell) => `${cell.address}: ${cell.text}`)]);
  const hasText = typeof preview.text === 'string'
    ? preview.text.length > 0
    : preview.format === 'CSV'
      ? preview.rows.some((row) => row.some((cell) => cell.text.length > 0))
      : preview.sheets.some((sheet) => sheet.cells.length > 0);
  return hasText ? content : ['No supported literal text was found.'];
};

function makeToken(used) {
  let token;
  do {
    const bytes = new Uint16Array(1);
    if (globalThis.crypto?.getRandomValues) globalThis.crypto.getRandomValues(bytes);
    else bytes[0] = Math.floor(Math.random() * 65536);
    token = `T_${String(bytes[0] % 1000).padStart(3, '0')}`;
  } while (used.has(token));
  used.add(token);
  return token;
}

export default function App() {
  const [files, setFiles] = useState(initialFiles);
  const [activeName, setActiveName] = useState(initialFiles[0].name);
  const [level, setLevel] = useState(() => {
    const saved = Number(localStorage.getItem('blot-level'));
    return saved >= 1 && saved <= 10 ? saved : 5;
  });
  const [decisionSets, setDecisionSets] = useState({});
  const [confirmationSets, setConfirmationSets] = useState({});
  const [selectedGroup, setSelectedGroup] = useState('alex');
  const [view, setView] = useState('preview');
  const [denseText, setDenseText] = useState(false);
  const [sectionIndex, setSectionIndex] = useState(0);
  const [pagePreviewDocument, setPagePreviewDocument] = useState(null);
  const [pagePreviewLoading, setPagePreviewLoading] = useState(false);
  const [pagePreviewError, setPagePreviewError] = useState('');
  const [pagePreviewHighlights, setPagePreviewHighlights] = useState([]);
  const pagePreviewCanvas = useRef(null);
  const pagePreviewFrame = useRef(null);
  const pagePreviewTextLayer = useRef(null);
  const pagePreviewHighlightLayer = useRef(null);
  const [theme, setTheme] = useState(() => localStorage.getItem('blot-theme') === 'dark' ? 'dark' : 'light');
  const [modalOpen, setModalOpen] = useState(false);
  const [projectModalOpen, setProjectModalOpen] = useState(false);
  const [projectAction, setProjectAction] = useState('create');
  const [projectName, setProjectName] = useState('');
  const [projectDirectory, setProjectDirectory] = useState('');
  const [localToken, setLocalToken] = useState('');
  const [serviceAvailable, setServiceAvailable] = useState(false);
  const [currentProject, setCurrentProject] = useState(null);
  const [recentProjects, setRecentProjects] = useState(readRecentProjects);
  const [projectBusy, setProjectBusy] = useState(false);
  const [exportPreview, setExportPreview] = useState(null);
  const [exportAcknowledged, setExportAcknowledged] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);
  const [restorePreviewData, setRestorePreviewData] = useState(null);
  const [restoreBusy, setRestoreBusy] = useState(false);
  const [backupDialog, setBackupDialog] = useState(null);
  const [backupPassphrase, setBackupPassphrase] = useState('');
  const [backupDirectory, setBackupDirectory] = useState('');
  const [backupBusy, setBackupBusy] = useState(false);
  const [ollayaRows, setOllayaRows] = useState([]);
  const [ollayaCsvOpen, setOllayaCsvOpen] = useState(false);
  const [ollayaCsvFilename, setOllayaCsvFilename] = useState('');
  const [ollayaCsvStatus, setOllayaCsvStatus] = useState('');
  const [ollayaCsvError, setOllayaCsvError] = useState('');
  const [ollayaCsvDirty, setOllayaCsvDirty] = useState(false);
  const [ollayaCsvSaving, setOllayaCsvSaving] = useState(false);
  const [ollayaCacheClearing, setOllayaCacheClearing] = useState(false);
  const [ollayaColumnWidths, setOllayaColumnWidths] = useState({
    word: 180,
    is_identifier_percent: 190,
    is_operationally_significant_percent: 245,
    is_common_word_percent: 190,
    is_correct: 110,
  });
  const csvLoadSequence = useRef(0);
  const csvColumnResize = useRef(null);
  const activeProjectRestoreAttempted = useRef(false);
  const [manualPhrase, setManualPhrase] = useState('');
  const [mergeGroupIds, setMergeGroupIds] = useState([]);
  const [selectedCandidateIds, setSelectedCandidateIds] = useState([]);
  const [bulkSelectedCandidateIds, setBulkSelectedCandidateIds] = useState([]);
  const [candidateBulkBusy, setCandidateBulkBusy] = useState(false);
  const [signalThresholds, setSignalThresholds] = useState({
    isIdentifier: 80,
    isOperationallySignificant: 80,
  });
  const [toast, setToast] = useState('');
  const [undo, setUndo] = useState(null);
  const [termContextMenu, setTermContextMenu] = useState(null);
  const termClickTimers = useRef(new Map());
  const [, setReplacementMaps] = useState({});

  const activeFile = files.find((file) => (file.id || file.name) === activeName) || files[0] || initialFiles[0];
  const projectDocuments = files.filter((file) => file.isProjectDocument);
  const projectHasNoDocuments = Boolean(currentProject && projectDocuments.length === 0);
  const activeVersion = activeFile?.versions?.find((version) => version.id === activeFile.selectedVersionId)
    || { id: activeFile?.versionId, kind: 'original', status: 'ready' };
  const reviewableProjectDocument = Boolean(activeFile?.isProjectDocument && activeVersion.kind === 'original');
  const officeFileNeedsProject = Boolean(!activeFile?.isProjectDocument && /\.(docx|xlsx|pptx)$/i.test(activeFile?.name || ''));
  const canStartExport = reviewableProjectDocument || (
    !activeFile?.isProjectDocument && !currentProject && /\.(txt|md)$/i.test(activeFile?.name || '')
  );
  const activeText = activeFile.rawText ?? activeFile.content.join(activeFile.lineEnding || '\n');
  const previewSections = activeFile.previewSections || [];
  const activeSection = previewSections[sectionIndex];
  const decisions = decisionSets[activeName] || { alex: 'suggested', cedar: 'suggested' };
  const confirmed = confirmationSets[activeName] || { alex: true, cedar: false };
  const getActiveMembers = (group) => confirmed[group.id] ? group.members : group.members.slice(0, 1);
  const visibleGroups = activeFile.isProjectDocument ? [] : groups.filter((group) => group.level >= 2 && group.level <= level && getActiveMembers(group).some((member) => countMatches(activeText, member)));
  const changeRows = visibleGroups.flatMap((group) => {
    const members = getActiveMembers(group);
    const occurrences = countGroupMatches(activeText, members);
    return occurrences ? [{ group, members, occurrences, decision: decisions[group.id] }] : [];
  });
  const candidates = activeFile.candidates || [];
  const queriedCandidateCount = candidates.filter((candidate) => (
    ['complete', 'partial', 'unavailable'].includes(candidate.scoreStatus)
  )).length;
  const matchCount = reviewableProjectDocument
    ? candidates
      .filter((candidate) => candidate.level >= 2 && candidate.level <= level && isCandidateAutoSuggested(candidate))
      .reduce((sum, candidate) => sum + candidate.occurrenceCount, 0)
    : changeRows.reduce((sum, row) => sum + (row.decision === 'excluded' ? 0 : row.occurrences), 0);
  const visibleCandidates = reviewableProjectDocument
    ? getVisibleCandidates(candidates, level)
    : [];
  const reviewCandidates = reviewableProjectDocument ? getReviewCandidates(candidates) : [];
  const includedCandidates = reviewableProjectDocument ? getIncludedCandidates(candidates) : [];
  const levelCandidates = visibleCandidates;
  const candidatesById = Object.fromEntries(candidates.map((candidate) => [candidate.id, candidate]));
  const candidateGroups = activeFile.candidateGroups || [];
  const groupedCandidateIds = new Set(candidateGroups.flatMap((group) => group.candidateIds));
  const candidateDecisionCounts = getCandidateDecisionCounts(candidates);
  const notIncludedByLevelCount = getCandidatesNotSelectedAtLevel(candidates, level);
  const previewCoverage = activeFile.previewCoverage;
  const previewWarnings = activeFile.previewWarnings || [];
  const unsupportedPartCount = previewCoverage?.unsupportedPartCount || 0;
  const exportPreviewText = exportPreview ? previewLines(exportPreview.preview).join('\n') : '';

  const editOllayaCsvCell = (rowIndex, column, value) => {
    setOllayaRows((current) => current.map((row, index) => (
      index === rowIndex
        ? { ...row, [column]: value, _dirty: true }
        : row
    )));
    setOllayaCsvDirty(true);
    setOllayaCsvStatus('Unsaved edits');
  };

  const reloadOllayaCsv = async (documentId, directory, { merge = true } = {}) => {
    if (!documentId || !directory || !localToken) return;
    const loadSequence = ++csvLoadSequence.current;
    setOllayaCsvStatus('Loading CSV…');
    setOllayaCsvError('');
    if (!merge) {
      setOllayaRows([]);
      setOllayaCsvDirty(false);
    }
    try {
      const query = new URLSearchParams({ directory, document_id: documentId });
      const response = await fetch(`/api/projects/ollaya-results?${query}`, {
        headers: { 'X-Local-App-Token': localToken },
        cache: 'no-store',
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not load Ollaya CSV');
      if (loadSequence !== csvLoadSequence.current) return;
      const loadedRows = data.rows.map((row) => ({
        ...row,
        _originalWord: row.word,
        _documentId: documentId,
        _dirty: false,
        _stream: false,
      }));
      setOllayaRows((current) => {
        if (!merge) return loadedRows;
        const rowsByWord = new Map(loadedRows.map((row) => [row.word.toLocaleLowerCase(), row]));
        current.filter((row) => row._documentId === documentId).forEach((row) => {
          const key = (row._originalWord || row.word).toLocaleLowerCase();
          const saved = rowsByWord.get(key);
          if (!saved) {
            if (row._dirty || row._stream) rowsByWord.set(key, row);
            return;
          }
          rowsByWord.set(key, {
            ...saved,
            ...(row._dirty ? row : row._stream ? {
              is_identifier_percent: row.is_identifier_percent,
              is_operationally_significant_percent: row.is_operationally_significant_percent,
              is_common_word_percent: row.is_common_word_percent,
            } : {}),
            is_correct: row._dirty ? row.is_correct : saved.is_correct,
            _originalWord: saved.word,
          });
        });
        return [...rowsByWord.values()];
      });
      setOllayaCsvFilename(data.filename);
      setOllayaCsvStatus('Saved locally');
      setOllayaCsvDirty(false);
    } catch (error) {
      if (loadSequence === csvLoadSequence.current) {
        setOllayaCsvStatus('');
        setOllayaCsvError(error.message || 'Could not load Ollaya CSV');
      }
    }
  };

  const saveOllayaCsv = async () => {
    if (!activeFile?.id || !projectDirectory || !localToken || ollayaCsvSaving) return;
    setOllayaCsvSaving(true);
    setOllayaCsvStatus('Saving…');
    setOllayaCsvError('');
    try {
      const rows = ollayaRows.map((row) => Object.fromEntries(OLLAYA_CSV_COLUMNS.map(([name]) => [
        name,
        name.endsWith('_percent') && row[name] !== '' && row[name] != null ? Number(row[name]) : row[name],
      ])));
      const response = await fetch('/api/projects/ollaya-results', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({
          directory: projectDirectory,
          document_id: activeFile.id,
          rows,
          original_words: ollayaRows.map((row) => row._originalWord || row.word),
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not save Ollaya CSV');
      setOllayaRows(data.rows.map((row) => ({
        ...row,
        _originalWord: row.word,
        _documentId: activeFile.id,
        _dirty: false,
        _stream: false,
      })));
      setOllayaCsvDirty(false);
      setOllayaCsvStatus('Saved locally');
    } catch (error) {
      setOllayaCsvStatus('');
      setOllayaCsvError(error.message || 'Could not save Ollaya CSV');
    } finally {
      setOllayaCsvSaving(false);
    }
  };

  const clearOllayaCache = async () => {
    if (!activeFile?.id || !projectDirectory || !localToken || ollayaCacheClearing || activeFile.candidateLoading) return;
    const unsavedWarning = ollayaCsvDirty ? ' Unsaved score edits will be cleared.' : '';
    if (!window.confirm(`Clear cached Ollaya responses for ${activeFile.name} and rescore this document now? Saved is_correct labels will be kept.${unsavedWarning}`)) return;

    setOllayaCacheClearing(true);
    setOllayaCsvError('');
    setOllayaCsvStatus('Clearing cache and rescoring…');
    csvLoadSequence.current += 1;
    setOllayaRows([]);
    setOllayaCsvDirty(false);
    try {
      const query = new URLSearchParams({ directory: projectDirectory, document_id: activeFile.id });
      const response = await fetch(`/api/projects/ollaya-results?${query}`, {
        method: 'DELETE',
        headers: { 'X-Local-App-Token': localToken },
        cache: 'no-store',
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not clear the Ollaya response cache');
      const rescored = await loadProjectDocumentCandidates(activeFile.id, [], projectDirectory);
      await reloadOllayaCsv(activeFile.id, projectDirectory, { merge: false });
      setToast(rescored
        ? `Cleared ${data.clearedCount} cached Ollaya response${data.clearedCount === 1 ? '' : 's'} and rescored ${activeFile.name}`
        : `Cleared cached Ollaya responses, but ${activeFile.name} could not be rescored`);
    } catch (error) {
      setOllayaCsvStatus('');
      setOllayaCsvError(error.message || 'Could not clear the Ollaya response cache');
      setToast(error.message || 'Could not clear the Ollaya response cache');
    } finally {
      setOllayaCacheClearing(false);
    }
  };

  const closeOllayaCsv = () => {
    if (ollayaCsvDirty && !window.confirm('Discard unsaved CSV edits?')) return;
    setOllayaCsvOpen(false);
    if (ollayaCsvDirty && activeFile?.id) void reloadOllayaCsv(activeFile.id, projectDirectory, { merge: false });
    setOllayaCsvDirty(false);
  };

  const startOllayaColumnResize = (column, event) => {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    csvColumnResize.current = {
      column,
      startX: event.clientX,
      startWidth: ollayaColumnWidths[column],
      pointerId: event.pointerId,
    };
  };

  const moveOllayaColumnResize = (event) => {
    const resize = csvColumnResize.current;
    if (!resize || resize.pointerId !== event.pointerId) return;
    setOllayaColumnWidths((current) => ({
      ...current,
      [resize.column]: Math.max(80, Math.min(700, resize.startWidth + event.clientX - resize.startX)),
    }));
  };

  const endOllayaColumnResize = (event) => {
    if (csvColumnResize.current?.pointerId === event.pointerId) csvColumnResize.current = null;
  };

  useEffect(() => {
    let cancelled = false;
    fetch('/api/bootstrap', { cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) throw new Error('Local service unavailable');
        return response.json();
      })
      .then(({ localAppToken: token }) => {
        if (cancelled) return;
        setLocalToken(token);
        setServiceAvailable(true);
      })
      .catch(() => {
        if (!cancelled) setServiceAvailable(false);
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!currentProject || !projectDirectory || !localToken || !activeFile?.isProjectDocument) {
      setOllayaRows([]);
      setOllayaCsvFilename('');
      setOllayaCsvError('');
      return undefined;
    }
    void reloadOllayaCsv(activeFile.id, projectDirectory);
    return () => { csvLoadSequence.current += 1; };
  }, [currentProject?.id, activeFile?.id, activeFile?.isProjectDocument, projectDirectory, localToken]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('blot-theme', theme);
  }, [theme]);
  useEffect(() => {
    localStorage.setItem('blot-level', String(level));
  }, [level]);
  useEffect(() => {
    const captureSelectedPhrase = () => {
      if (!reviewableProjectDocument) return;
      const selection = window.getSelection();
      const selected = selection?.toString().trim() || '';
      const page = document.querySelector('.doc-page, .docx-text-layer');
      if (selected && selected.length <= 256 && page?.contains(selection.anchorNode)) {
        setManualPhrase(selected);
      }
    };
    document.addEventListener('mouseup', captureSelectedPhrase);
    return () => document.removeEventListener('mouseup', captureSelectedPhrase);
  }, [reviewableProjectDocument]);

  useEffect(() => {
    const pdfUrl = activeFile?.pagePreviewUrl;
    if (!pdfUrl) {
      setPagePreviewDocument(null);
      setPagePreviewLoading(false);
      setPagePreviewError(activeFile?.pagePreviewError || '');
      setPagePreviewHighlights([]);
      return undefined;
    }

    let cancelled = false;
    let loadingTask;
    setPagePreviewDocument(null);
    setPagePreviewLoading(true);
    setPagePreviewError('');
    import('pdfjs-dist').then((pdfjsLib) => {
      if (cancelled) return null;
      pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorker;
      loadingTask = pdfjsLib.getDocument({ url: pdfUrl });
      return loadingTask.promise;
    }).then((pdf) => {
      if (!pdf) return;
      if (cancelled) {
        void pdf.destroy();
        return;
      }
      setPagePreviewDocument({ url: pdfUrl, pdf });
      setPagePreviewLoading(false);
      setFiles((current) => current.map((file) => (
        file.id === activeFile.id ? { ...file, pagePreviewPageCount: pdf.numPages } : file
      )));
    }).catch((error) => {
      if (cancelled) return;
      setPagePreviewLoading(false);
      setPagePreviewError(error.message || 'The rendered page preview could not be opened.');
    });
    return () => {
      cancelled = true;
      void loadingTask?.destroy();
    };
  }, [activeFile?.id, activeFile?.pagePreviewUrl]);

  useEffect(() => {
    const pdf = pagePreviewDocument?.pdf;
    const canvas = pagePreviewCanvas.current;
    if (!pdf || !canvas || pagePreviewDocument.url !== activeFile?.pagePreviewUrl) return undefined;

    let cancelled = false;
    let renderTask;
    let textLayer;
    const renderPage = async () => {
      const pageNumber = Math.min(Math.max(activeFile.pagePreviewPage || 1, 1), pdf.numPages);
      const page = await pdf.getPage(pageNumber);
      if (cancelled) return;
      const baseViewport = page.getViewport({ scale: 1 });
      const frame = pagePreviewFrame.current;
      const textLayerElement = pagePreviewTextLayer.current;
      if (!frame || !textLayerElement) return;
      const availableWidth = Math.max(240, (frame.parentElement?.clientWidth || baseViewport.width) - 40);
      const scale = Math.min(1.5, availableWidth / baseViewport.width);
      const viewport = page.getViewport({ scale });
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      frame.style.width = `${Math.ceil(viewport.width)}px`;
      frame.style.height = `${Math.ceil(viewport.height)}px`;
      textLayerElement.replaceChildren();
      setPagePreviewHighlights([]);
      canvas.width = Math.ceil(viewport.width * pixelRatio);
      canvas.height = Math.ceil(viewport.height * pixelRatio);
      canvas.style.width = `${Math.ceil(viewport.width)}px`;
      canvas.style.height = `${Math.ceil(viewport.height)}px`;
      renderTask = page.render({
        canvasContext: canvas.getContext('2d'),
        viewport,
        transform: pixelRatio === 1 ? null : [pixelRatio, 0, 0, pixelRatio, 0, 0],
      });
      const [textContent] = await Promise.all([page.getTextContent(), renderTask.promise]);
      if (cancelled) return;
      const pdfjsLib = await import('pdfjs-dist');
      textLayer = new pdfjsLib.TextLayer({
        textContentSource: textContent,
        container: textLayerElement,
        viewport,
      });
      await textLayer.render();
      if (cancelled) return;
      const nodes = textLayer.textDivs.flatMap((div) => {
        const walker = document.createTreeWalker(div, NodeFilter.SHOW_TEXT);
        const found = [];
        while (walker.nextNode()) found.push(walker.currentNode);
        return found;
      });
      const fullText = nodes.map((node) => node.textContent).join('');
      const terms = reviewableProjectDocument && !activeFile.candidateLoading
        ? getVisibleCandidates(activeFile.candidates || [], level).filter((candidate) => (
          isCandidateAutoSuggested(candidate) || candidate.decision === 'excluded'
        ))
        : [];
      const matches = findPageTermMatches(fullText, terms);
      const nodeRanges = [];
      let nodeOffset = 0;
      nodes.forEach((node) => {
        nodeRanges.push({ node, start: nodeOffset, end: nodeOffset + node.textContent.length });
        nodeOffset += node.textContent.length;
      });
      const locate = (offset, preferPrevious = false) => {
        const entry = nodeRanges.find(({ start, end }) => (
          (offset >= start && offset < end) || (preferPrevious && offset === end)
        ));
        if (!entry) return null;
        return { node: entry.node, offset: offset - entry.start };
      };
      const highlightLayer = pagePreviewHighlightLayer.current;
      const layerBounds = highlightLayer?.getBoundingClientRect();
      if (!layerBounds) return;
      const layerSize = {
        width: highlightLayer.clientWidth || layerBounds.width,
        height: highlightLayer.clientHeight || layerBounds.height,
      };
      const highlights = [];
      matches.forEach(({ start, end, candidate }, matchIndex) => {
        const rangeStart = locate(start);
        const rangeEnd = locate(end, true);
        if (!rangeStart || !rangeEnd) return;
        const range = document.createRange();
        range.setStart(rangeStart.node, rangeStart.offset);
        range.setEnd(rangeEnd.node, rangeEnd.offset);
        [...range.getClientRects()].forEach((rect, fragmentIndex) => {
          if (!rect.width || !rect.height) return;
          const box = mapClientRectToLayer(rect, layerBounds, layerSize);
          highlights.push({
            candidate,
            matchIndex,
            fragmentIndex,
            ...box,
          });
        });
      });
      setPagePreviewHighlights(highlights);
    };
    void renderPage().catch((error) => {
      if (!cancelled && error.name !== 'RenderingCancelledException') {
        setPagePreviewError(error.message || 'This page could not be rendered.');
      }
    });
    return () => {
      cancelled = true;
      renderTask?.cancel();
      textLayer?.cancel();
    };
  }, [activeFile?.pagePreviewPage, activeFile?.pagePreviewUrl, activeFile?.candidates, level, pagePreviewDocument, reviewableProjectDocument]);

  useEffect(() => {
    if (!toast) return undefined;
    const timer = window.setTimeout(() => setToast(''), 2600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => () => {
    termClickTimers.current.forEach((timer) => window.clearTimeout(timer));
    termClickTimers.current.clear();
  }, []);

  useEffect(() => {
    if (!termContextMenu) return undefined;
    const dismissMenu = (event) => {
      if (event.type === 'keydown' && event.key !== 'Escape') return;
      if (event.type === 'pointerdown' && event.target.closest?.('[data-term-context-menu]')) return;
      setTermContextMenu(null);
    };
    document.addEventListener('pointerdown', dismissMenu);
    document.addEventListener('keydown', dismissMenu);
    return () => {
      document.removeEventListener('pointerdown', dismissMenu);
      document.removeEventListener('keydown', dismissMenu);
    };
  }, [termContextMenu]);

  useEffect(() => {
    setTermContextMenu(null);
  }, [activeName, view]);

  const changeDecision = (groupId, decision) => {
    setUndo({ file: activeName, groupId, decision: decisions[groupId], confirmed: confirmed[groupId] });
    setDecisionSets((current) => ({ ...current, [activeName]: { ...(current[activeName] || { alex: 'suggested', cedar: 'suggested' }), [groupId]: decision } }));
    if (decision === 'included') setConfirmationSets((current) => ({ ...current, [activeName]: { ...(current[activeName] || { alex: true, cedar: false }), [groupId]: true } }));
    const group = groups.find((item) => item.id === groupId);
    const label = decision === 'included' ? 'Included' : decision === 'excluded' ? 'Excluded' : 'Reset';
    setToast(`${label} ${group.term} group across this document`);
  };

  const applyTermDecision = (target, decision) => {
    if (target.kind === 'candidate') {
      void setProjectCandidateDecision(target.candidate, decision);
    } else {
      changeDecision(target.id, decision);
    }
    setTermContextMenu(null);
  };

  const getTermClickKey = (target) => `${activeName}:${target.kind}:${target.kind === 'candidate' ? target.candidate.id : target.id}`;

  const clearTermClickTimer = (target) => {
    const key = getTermClickKey(target);
    const timer = termClickTimers.current.get(key);
    if (timer) window.clearTimeout(timer);
    termClickTimers.current.delete(key);
    return key;
  };

  const handleTermClick = (event, target) => {
    if (event.detail === 0) {
      clearTermClickTimer(target);
      applyTermDecision(target, 'included');
      return;
    }
    if (event.detail > 1) {
      clearTermClickTimer(target);
      return;
    }
    const key = clearTermClickTimer(target);
    const timer = window.setTimeout(() => {
      termClickTimers.current.delete(key);
      applyTermDecision(target, 'included');
    }, 500);
    termClickTimers.current.set(key, timer);
  };

  const handleTermDoubleClick = (event, target) => {
    event.preventDefault();
    clearTermClickTimer(target);
    applyTermDecision(target, 'excluded');
  };

  const openTermContextMenu = (event, target) => {
    event.preventDefault();
    clearTermClickTimer(target);
    setTermContextMenu({
      target,
      fileName: activeName,
      x: Math.max(8, Math.min(event.clientX, window.innerWidth - 170)),
      y: Math.max(8, Math.min(event.clientY, window.innerHeight - 124)),
    });
  };

  const findPagePreviewTarget = (event) => {
    const indexedButton = event.target.closest?.('[data-page-term-index]');
    if (indexedButton) {
      return pagePreviewHighlights[Number(indexedButton.dataset.pageTermIndex)]?.candidate;
    }
    const highlightLayer = pagePreviewHighlightLayer.current;
    const layerBounds = highlightLayer?.getBoundingClientRect();
    if (!highlightLayer || !layerBounds) return null;
    const point = mapClientPointToLayer(event.clientX, event.clientY, layerBounds, {
      width: highlightLayer.clientWidth || layerBounds.width,
      height: highlightLayer.clientHeight || layerBounds.height,
    });
    return pagePreviewHighlights.find((highlight) => (
      point.x >= highlight.left && point.x <= highlight.left + highlight.width
      && point.y >= highlight.top && point.y <= highlight.top + highlight.height
    ))?.candidate || null;
  };

  const handlePagePreviewClick = (event) => {
    const candidate = findPagePreviewTarget(event);
    if (candidate) handleTermClick(event, { kind: 'candidate', candidate });
  };

  const handlePagePreviewDoubleClick = (event) => {
    const candidate = findPagePreviewTarget(event);
    if (candidate) handleTermDoubleClick(event, { kind: 'candidate', candidate });
  };

  const handlePagePreviewContextMenu = (event) => {
    const candidate = findPagePreviewTarget(event);
    if (candidate) openTermContextMenu(event, { kind: 'candidate', candidate });
  };

  const renderParagraph = (paragraph) => {
    const terms = (reviewableProjectDocument && !activeFile.candidateLoading
      ? levelCandidates.map((candidate) => ({ member: candidate.term, candidate }))
      : activeFile.isProjectDocument
        ? []
        : groups.flatMap((group) => getActiveMembers(group).map((member) => ({ member, group })))
    ).sort((a, b) => b.member.length - a.member.length);
    if (!terms.length) return paragraph;
    const regex = new RegExp(`(${terms.map(({ member }) => member.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
    return paragraph.split(regex).map((part, index) => {
      const found = terms.find(({ member }) => member.toLowerCase() === part.toLowerCase());
      if (!found) return part;
      if (found.candidate) {
        const { candidate } = found;
        if (!isCandidateAutoSuggested(candidate) && candidate.decision !== 'excluded') return part;
        const target = { kind: 'candidate', candidate };
        const className = candidate.decision === 'excluded'
          ? 'excluded'
          : candidate.decision === 'included'
            ? 'included'
            : 'auto';
        return <button key={`${candidate.id}-${index}`} type="button" className={`term ${className}`} onClick={(event) => handleTermClick(event, target)} onDoubleClick={(event) => handleTermDoubleClick(event, target)} onContextMenu={(event) => openTermContextMenu(event, target)} aria-label={`${part}: ${candidate.decision}; click to include, double-click to exclude, or right-click for options`}>{part}</button>;
      }
      const { group } = found;
      const decision = decisions[group.id];
      const isCandidate = group.level >= 2 && group.level <= level;
      const className = isCandidate ? (decision === 'excluded' ? 'excluded' : decision === 'included' ? 'included' : 'auto') : '';
      if (!className) return part;
      const target = { kind: 'group', id: group.id };
      return <button key={`${group.id}-${index}`} type="button" className={`term ${className}`} onClick={(event) => handleTermClick(event, target)} onDoubleClick={(event) => handleTermDoubleClick(event, target)} onContextMenu={(event) => openTermContextMenu(event, target)} aria-label={`${part}: ${decision}; click to include, double-click to exclude, or right-click for options`}>{part}</button>;
    });
  };

  const loadProjectDocumentCandidates = async (documentId, manualTerms = [], directory = projectDirectory) => {
    if (!directory || !localToken) return false;
    setExportPreview(null);
    setFiles((current) => current.map((file) => (
      file.id === documentId
        ? { ...file, candidates: [], candidateLoading: true, candidateProgress: 0, candidateError: '' }
        : file
    )));
    setSelectedCandidateIds([]);
    setBulkSelectedCandidateIds([]);
    try {
      const response = await fetch('/api/projects/document-candidates/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({ directory, document_id: documentId, manual_terms: manualTerms }),
      });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || 'Could not analyze supported text');
      }
      if (!response.body) throw new Error('This browser does not support streamed candidate analysis');

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let completed = false;
      const handleEvent = (line) => {
        if (!line.trim()) return;
        const event = JSON.parse(line);
        if (event.type === 'candidate') {
          if (event.candidate.scoreStatus !== 'queued' && documentId === activeName) {
            const csvRow = csvRowFromCandidate(event.candidate);
            setOllayaRows((current) => {
              const rowIndex = current.findIndex((row) => (
                (row._originalWord || row.word).toLocaleLowerCase() === csvRow.word.toLocaleLowerCase()
              ));
              if (rowIndex < 0) return [...current, {
                ...csvRow,
                _originalWord: csvRow.word,
                _documentId: documentId,
                _stream: true,
              }];
              return current.map((row, index) => index === rowIndex
                ? {
                  ...(row._dirty ? row : csvRow),
                  is_correct: row.is_correct || 'N',
                  _originalWord: row._originalWord || row.word,
                  _documentId: documentId,
                  _stream: true,
                }
                : row);
            });
            setOllayaCsvStatus('Saved locally');
          }
          setFiles((current) => current.map((file) => {
            if (file.id !== documentId) return file;
            const currentCandidates = file.candidates || [];
            const isNew = !currentCandidates.some((candidate) => candidate.id === event.candidate.id);
            return {
              ...file,
              candidates: upsertCandidate(currentCandidates, event.candidate),
              candidateProgress: (file.candidateProgress || 0) + Number(isNew),
            };
          }));
          return;
        }
        if (event.type === 'error') throw new Error(event.detail || 'Could not analyze supported text');
        if (event.type !== 'complete') return;
        const { data } = event;
        setFiles((current) => current.map((file) => (
          file.id === documentId
            ? {
              ...file,
              candidates: data.candidates,
              candidateGroups: data.groups,
              candidateLimitReached: data.candidateLimitReached,
              ollayaWarning: data.ollayaWarning || '',
              candidateLoaded: true,
              candidateLoading: false,
              candidateError: '',
            }
            : file
        )));
        completed = true;
      };

      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        lines.forEach(handleEvent);
        if (done) break;
      }
      if (buffer.trim()) handleEvent(buffer);
      if (!completed) throw new Error('Candidate analysis ended before it was complete');
      return true;
    } catch (error) {
      setFiles((current) => current.map((file) => (
        file.id === documentId ? { ...file, candidateLoading: false, candidateLoaded: false, candidateError: error.message } : file
      )));
      return false;
    }
  };

  const loadProjectDocumentPreview = async (documentId, versionId, versionKind, directory = projectDirectory) => {
    if (!directory || !localToken) return;
    setFiles((current) => current.map((file) => (
      file.id === documentId ? { ...file, previewLoading: true } : file
    )));
    try {
      const response = await fetch('/api/projects/document-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({ directory, document_id: documentId, version_id: versionId || undefined }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not parse this document for preview');
      const content = previewLines(data);
      let pagePreviewUrl;
      let pagePreviewError = '';
      if (data.format === 'DOCX') {
        setPagePreviewLoading(true);
        try {
          const pageResponse = await fetch('/api/projects/document-page-preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
            body: JSON.stringify({ directory, document_id: documentId, version_id: versionId || undefined }),
          });
          if (!pageResponse.ok) {
            const pageError = await pageResponse.json();
            throw new Error(pageError.detail || 'The DOCX page preview is not available.');
          }
          pagePreviewUrl = URL.createObjectURL(await pageResponse.blob());
        } catch (error) {
          pagePreviewError = error.message || 'The DOCX page preview is not available.';
        } finally {
          setPagePreviewLoading(false);
        }
      }
      const previewSections = (data.previewSections || []).map((section) => {
        const slideMatch = data.format === 'PPTX' && section.part.match(/(?:^|\/)slide(\d+)\.xml$/i);
        return {
          label: slideMatch ? `Slide ${slideMatch[1]}` : section.part,
          content: section.text.split(/\r\n|\r|\n/),
        };
      });
      const knownFile = files.find((file) => file.id === documentId);
      if (knownFile?.pagePreviewUrl && knownFile.pagePreviewUrl !== pagePreviewUrl) {
        URL.revokeObjectURL(knownFile.pagePreviewUrl);
      }
      const selectedVersionId = versionId || knownFile?.selectedVersionId || knownFile?.versionId;
      const selectedVersion = knownFile?.versions?.find((version) => version.id === selectedVersionId)
        || { kind: versionKind || 'original' };
      setFiles((current) => current.map((file) => (
        file.id === documentId
          ? {
            ...file,
            content: previewSections[0]?.content || (content.length ? content : ['No supported literal text was found in this document.']),
            previewSections,
            previewSectionsTruncated: data.previewSectionsTruncated || false,
            rawText: data.text ?? content.join('\n'),
            previewFormat: data.format,
            previewEncoding: data.encoding,
            previewWarnings: data.warnings || [],
            previewCoverage: data.coverage || null,
            previewLineEndings: data.lineEndings || null,
            previewDialect: data.dialect || null,
            existingPlaceholderLikeTextCount: data.existingPlaceholderLikeTextCount || 0,
            previewTruncated: data.truncated,
            previewError: false,
            pagePreviewUrl,
            pagePreviewError,
            pagePreviewPage: 1,
            pagePreviewPageCount: undefined,
            previewLoaded: true,
            previewLoading: false,
            selectedVersionId,
            status: selectedVersion?.kind === 'obfuscated'
              ? 'Obfuscated copy · original unchanged'
              : selectedVersion?.kind === 'restored'
                ? 'Restored copy · originals unchanged'
                : 'Parsed preview · original unchanged',
            restoreReport: data.restoreReport || null,
          }
          : file
      )));
      if ((selectedVersion?.kind || 'original') === 'original') await loadProjectDocumentCandidates(documentId, [], directory);
    } catch (error) {
      setFiles((current) => current.map((file) => (
        file.id === documentId
          ? { ...file, content: [`Preview unavailable: ${error.message}`], previewLoading: false, previewLoaded: true, previewError: true }
          : file
      )));
      setToast(error.message || 'Could not parse this document for preview');
    }
  };

  const selectPreviewSection = (index) => {
    if (!previewSections.length) return;
    const nextIndex = Math.min(Math.max(index, 0), previewSections.length - 1);
    setSectionIndex(nextIndex);
    setFiles((current) => current.map((file) => (
      file.id === activeFile.id
        ? { ...file, content: previewSections[nextIndex].content }
        : file
    )));
  };

  const selectDocxPage = (pageNumber) => {
    const pageCount = activeFile.pagePreviewPageCount || 1;
    const nextPage = Math.min(Math.max(pageNumber, 1), pageCount);
    setFiles((current) => current.map((file) => (
      file.id === activeFile.id ? { ...file, pagePreviewPage: nextPage } : file
    )));
  };

  const selectProjectVersion = (versionId) => {
    const version = activeFile.versions?.find((item) => item.id === versionId);
    if (!version || version.id === activeFile.selectedVersionId) return;
    setExportPreview(null);
    setRestorePreviewData(null);
    setUndo(null);
    setSectionIndex(0);
    setDenseText(false);
    void loadProjectDocumentPreview(activeFile.id, version.id, version.kind);
  };

  const handlePreviewNavigationKeyDown = (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    let nextIndex = null;
    if (event.key === 'ArrowLeft') nextIndex = sectionIndex - 1;
    if (event.key === 'ArrowRight') nextIndex = sectionIndex + 1;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = previewSections.length - 1;
    if (nextIndex === null || nextIndex < 0 || nextIndex >= previewSections.length || nextIndex === sectionIndex) return;
    event.preventDefault();
    selectPreviewSection(nextIndex);
  };

  const setProjectCandidateDecision = async (candidate, decision) => {
    if (!activeFile.isProjectDocument || !localToken) return;
    try {
      const response = await fetch('/api/projects/candidate-decision', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({
          directory: projectDirectory,
          document_id: activeFile.id,
          candidate_id: candidate.id,
          decision,
        }),
      });
      const updated = await response.json();
      if (!response.ok) throw new Error(updated.detail || 'Could not save this candidate decision');
      setExportPreview(null);
      if (candidateGroups.some((group) => group.candidateIds.includes(candidate.id))) {
        await loadProjectDocumentCandidates(activeFile.id);
      } else {
        setFiles((current) => current.map((file) => (
          file.id === activeFile.id
            ? { ...file, candidates: upsertCandidate(file.candidates || [], updated) }
            : file
        )));
      }
      setUndo({ file: activeName, candidateId: candidate.id, decision: candidate.decision, isCandidate: true });
      setToast(`${decision === 'included' ? 'Included' : decision === 'excluded' ? 'Excluded' : 'Reset'} ${candidate.term}`);
    } catch (error) {
      setToast(error.message || 'Could not save this candidate decision');
    }
  };

  const applyBulkCandidateDecision = async (candidateIds, decision) => {
    const ids = [...new Set(candidateIds)];
    if (!ids.length || !activeFile.isProjectDocument || !localToken) return;
    setCandidateBulkBusy(true);
    try {
      const response = await fetch('/api/projects/candidate-decisions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({
          directory: projectDirectory,
          document_id: activeFile.id,
          candidate_ids: ids,
          decision,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not update candidate decisions');
      setExportPreview(null);
      setFiles((current) => current.map((file) => (
        file.id === activeFile.id ? { ...file, candidates: data.candidates } : file
      )));
      setBulkSelectedCandidateIds([]);
      setToast(`${decision === 'included' ? 'Included' : 'Excluded'} ${ids.length} candidate${ids.length === 1 ? '' : 's'}`);
    } catch (error) {
      setToast(error.message || 'Could not update candidate decisions');
    } finally {
      setCandidateBulkBusy(false);
    }
  };

  const selectCandidatesBySignal = (signalName) => {
    const selected = getCandidatesMatchingSignal(
      reviewCandidates,
      signalName,
      signalThresholds[signalName],
    ).map((candidate) => candidate.id);
    setBulkSelectedCandidateIds((current) => [...new Set([...current, ...selected])]);
    setToast(`Added matching candidates to the current selection`);
  };

  const addManualCandidate = async (event) => {
    event.preventDefault();
    const phrase = manualPhrase.trim();
    if (!phrase) return;
    setExportPreview(null);
    const saved = await loadProjectDocumentCandidates(activeFile.id, [phrase]);
    if (saved) {
      setManualPhrase('');
      setToast(`Added “${phrase}” as a pinned manual candidate`);
    } else {
      setToast(activeFile.candidateError || 'The phrase could not be added to this document version');
    }
  };

  const applyCandidateGroupOperation = async (operation, candidateIds, groupId, groupIds = []) => {
    try {
      const response = await fetch('/api/projects/candidate-groups', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({
          directory: projectDirectory,
          document_id: activeFile.id,
          operation,
          candidate_ids: candidateIds,
          group_id: groupId,
          group_ids: groupIds,
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not update confirmed groups');
      setExportPreview(null);
      setFiles((current) => current.map((file) => (
        file.id === activeFile.id ? { ...file, candidateGroups: data.groups } : file
      )));
      setMergeGroupIds([]);
      setSelectedCandidateIds([]);
      setToast(operation === 'add' ? 'Created a confirmed group. It remains separate from candidate Include/Exclude decisions.' : 'Updated confirmed group membership');
    } catch (error) {
      setToast(error.message || 'Could not update confirmed groups');
    }
  };

  const handleImport = async (event) => {
    if (currentProject) {
      setToast('Persistent document import is the next implementation stage. No project files were changed.');
      event.target.value = '';
      return;
    }
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    if (file.size > 100 * 1024 * 1024) {
      setToast('This file exceeds the 100 MB project limit');
      return;
    }
    const extension = file.name.split('.').pop()?.toLowerCase();
    if (!['txt', 'md', 'csv'].includes(extension)) {
      setToast('This React preview imports TXT, MD, and CSV. DOCX/PPTX need the local document engine.');
      return;
    }
    try {
      const content = await file.text();
      const imported = { name: file.name, type: extension.toUpperCase(), status: 'Imported · needs review', content: content.split(/\r?\n/), rawText: content, heading: file.name };
      setFiles((current) => [imported, ...current.filter((item) => item.name !== file.name)]);
      setActiveName(file.name);
      setDecisionSets((current) => ({ ...current, [file.name]: { alex: 'suggested', cedar: 'suggested' } }));
      setConfirmationSets((current) => ({ ...current, [file.name]: { alex: true, cedar: false } }));
      setUndo(null);
      setSelectedGroup('alex');
      setModalOpen(false);
      setToast(`${file.name} imported locally · editable text ready to review`);
    } catch {
      setToast('This file could not be read as text. The original is unchanged.');
    }
  };

  const importProjectDocuments = async () => {
    if (!currentProject || !projectDirectory || !localToken) {
      setToast('Open a local project and start the service before importing documents');
      return;
    }
    setProjectBusy(true);
    try {
      const pickerResponse = await fetch('/api/dialogs/document-files', {
        headers: { 'X-Local-App-Token': localToken },
        cache: 'no-store',
      });
      const selection = await pickerResponse.json();
      if (!pickerResponse.ok) throw new Error(selection.detail || 'Could not open the document picker');
      if (selection.cancelled) return;
      const response = await fetch('/api/projects/documents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({ directory: projectDirectory, files: selection.files }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not save the selected documents');
      const imported = data.documents.map((document) => ({
        id: document.id,
        versionId: document.versionId,
        versions: document.versions || [],
        selectedVersionId: document.versionId,
        name: document.name,
        type: document.type,
        status: 'Saved original · v01',
        content: ['Loading a local parsed preview…'],
        rawText: '',
        heading: `${document.name} · saved original`,
        isProjectDocument: true,
      }));
      setExportPreview(null);
      setFiles((current) => [...imported, ...current.filter((file) => file.isProjectDocument && !imported.some((document) => document.id === file.id))]);
      setActiveName(imported[0].id);
      setSectionIndex(0);
      setDenseText(false);
      setSelectedGroup('alex');
      setView('preview');
      await loadProjectDocumentPreview(imported[0].id);
      setToast(`${imported.length} original${imported.length === 1 ? '' : 's'} copied into the local project`);
    } catch (error) {
      setToast(error.message || 'Could not import the selected documents');
    } finally {
      setProjectBusy(false);
    }
  };

  const previewProjectExport = async () => {
    if (!reviewableProjectDocument || !currentProject || !localToken) {
      setToast('Select an original version in an open local project before preparing an export.');
      return;
    }
    if (!matchCount) {
      setToast(`No candidates are selected at level ${level}. Increase the level to obfuscate detected terms.`);
      return;
    }
    setExportBusy(true);
    setExportPreview(null);
    setExportAcknowledged(false);
    try {
      const response = await fetch('/api/projects/export-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({ directory: projectDirectory, document_id: activeFile.id, level }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not prepare an obfuscated preview');
      setExportPreview(data);
    } catch (error) {
      setToast(error.message || 'Could not prepare an obfuscated preview');
    } finally {
      setExportBusy(false);
    }
  };

  const approveProjectExport = async () => {
    if (!exportPreview || exportBusy) return;
    setExportBusy(true);
    try {
      const response = await fetch('/api/projects/export', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({
          directory: projectDirectory,
          document_id: activeFile.id,
          plan_id: exportPreview.planId,
          acknowledge_warnings: exportAcknowledged,
        }),
      });
      const version = await response.json();
      if (!response.ok) throw new Error(version.detail || 'The reviewed export could not be saved');
      setFiles((current) => current.map((file) => (
        file.id === activeFile.id
          ? {
            ...file,
            versions: [...(file.versions || []), version],
            selectedVersionId: version.id,
            status: 'Obfuscated copy saved · ready to send',
          }
          : file
      )));
      setExportPreview(null);
      setUndo(null);
      setSectionIndex(0);
      await loadProjectDocumentPreview(activeFile.id, version.id, 'obfuscated');
      setView('preview');
      if (await downloadProjectVersion(version.id, version.name)) {
        setToast('Approved obfuscated version saved and downloaded. The original remains unchanged.');
      }
    } catch (error) {
      setToast(error.message || 'The reviewed export could not be saved');
    } finally {
      setExportBusy(false);
    }
  };

  const downloadProjectVersion = async (versionId, filename) => {
    if (!activeFile.isProjectDocument || !currentProject || !localToken) return false;
    try {
      const downloadResponse = await fetch('/api/projects/document-version-download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({ directory: projectDirectory, document_id: activeFile.id, version_id: versionId }),
      });
      if (!downloadResponse.ok) {
        const data = await downloadResponse.json();
        throw new Error(data.detail || 'The saved obfuscated version could not be downloaded');
      }
      const objectUrl = URL.createObjectURL(await downloadResponse.blob());
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      anchor.download = filename;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      return true;
    } catch (error) {
      setToast(error.message || 'The saved obfuscated version could not be downloaded');
      return false;
    }
  };

  const exportCopy = () => {
    if (activeFile.isProjectDocument) {
      if (!reviewableProjectDocument) {
        setToast('Choose the saved original version before creating another obfuscated copy.');
        return;
      }
      void previewProjectExport();
      return;
    }
    if (currentProject) {
      setToast('Select a saved project document to create an immutable obfuscated version.');
      return;
    }
    if (!/\.(txt|md)$/i.test(activeFile.name)) {
      setToast('Import the original DOCX, XLSX, or PPTX into a local project to preserve its Office format during export.');
      return;
    }
    const used = new Set([...activeText.matchAll(/\[\[T_(\d+)\]\]/g)].map((match) => `T_${match[1]}`));
    let output = activeText;
    const map = new Map();
    for (const row of changeRows) {
      if (row.decision === 'excluded') continue;
      const token = makeToken(used);
      for (const member of row.members) {
        const escaped = member.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        output = output.replace(new RegExp(escaped, 'gi'), `[[${token}]]`);
      }
      map.set(token, row.group.term);
    }
    if (!map.size) {
      setToast('No candidates are selected at this obfuscation level. Increase the level to obfuscate detected terms.');
      return;
    }
    setReplacementMaps((current) => ({ ...current, [activeFile.name]: Object.fromEntries(map) }));
    const blob = new Blob([output], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    const baseName = activeFile.name.replace(/\.[^.]+$/, '');
    anchor.href = url;
    anchor.download = `${baseName}.obfuscated.txt`;
    anchor.click();
    URL.revokeObjectURL(url);
    setToast(`Obfuscated text copy downloaded · ${map.size} private replacement${map.size === 1 ? '' : 's'} kept in this session`);
  };

  const previewProjectRestoration = async () => {
    if (!currentProject || !activeFile.isProjectDocument || activeVersion.kind !== 'obfuscated' || !localToken) {
      setToast('Select an obfuscated DOCX, PPTX, or XLSX version in an open project first.');
      return;
    }
    setRestoreBusy(true);
    try {
      const pickerResponse = await fetch('/api/dialogs/document-files', {
        headers: { 'X-Local-App-Token': localToken },
        cache: 'no-store',
      });
      const selection = await pickerResponse.json();
      if (!pickerResponse.ok) throw new Error(selection.detail || 'Could not open the document picker');
      if (selection.cancelled || !selection.files?.length) return;
      const response = await fetch('/api/projects/restore-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({
          directory: projectDirectory,
          document_id: activeFile.id,
          source_version_id: activeVersion.id,
          returned_path: selection.files[0],
        }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not prepare the restoration preview');
      setRestorePreviewData(data);
    } catch (error) {
      setToast(error.message || 'Could not prepare the restoration preview');
    } finally {
      setRestoreBusy(false);
    }
  };

  const commitProjectRestoration = async () => {
    if (!restorePreviewData || restoreBusy) return;
    setRestoreBusy(true);
    try {
      const response = await fetch('/api/projects/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({ directory: projectDirectory, plan_id: restorePreviewData.planId }),
      });
      const version = await response.json();
      if (!response.ok) throw new Error(version.detail || 'Could not save the restored version');
      setFiles((current) => current.map((file) => (
        file.id === activeFile.id
          ? { ...file, versions: [...(file.versions || []), version], selectedVersionId: version.id, status: 'Restored copy saved · originals unchanged' }
          : file
      )));
      setRestorePreviewData(null);
      setUndo(null);
      setSectionIndex(0);
      await loadProjectDocumentPreview(activeFile.id, version.id, 'restored');
      setToast(`Restored ${version.restoreReport.restoredCount} exact placeholder occurrence${version.restoreReport.restoredCount === 1 ? '' : 's'}; ${version.restoreReport.unresolvedCount} unresolved remain.`);
    } catch (error) {
      setToast(error.message || 'Could not save the restored version');
    } finally {
      setRestoreBusy(false);
    }
  };

  const createPortableBackup = async (event) => {
    event.preventDefault();
    if (!currentProject || !backupPassphrase || !localToken) return;
    setBackupBusy(true);
    try {
      const response = await fetch('/api/projects/backup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({ directory: projectDirectory, passphrase: backupPassphrase }),
      });
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.detail || 'Could not create the encrypted backup');
      }
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `${currentProject.name.replace(/[^a-z0-9_-]+/gi, '-')}.blotbackup`;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      setBackupDialog(null);
      setBackupPassphrase('');
      setToast('Encrypted portable backup downloaded. Keep its passphrase separately.');
    } catch (error) {
      setToast(error.message || 'Could not create the encrypted backup');
    } finally {
      setBackupBusy(false);
    }
  };

  const chooseBackupDirectory = async () => {
    try {
      const response = await fetch('/api/dialogs/project-folder', {
        headers: { 'X-Local-App-Token': localToken },
        cache: 'no-store',
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not choose a destination folder');
      if (!data.cancelled) setBackupDirectory(data.directory);
    } catch (error) {
      setToast(error.message || 'Could not choose a destination folder');
    }
  };

  const restorePortableBackup = async (event) => {
    event.preventDefault();
    if (!backupDirectory || !backupPassphrase || !localToken) return;
    setBackupBusy(true);
    try {
      const backupPicker = await fetch('/api/dialogs/backup-file', {
        headers: { 'X-Local-App-Token': localToken },
        cache: 'no-store',
      });
      const selection = await backupPicker.json();
      if (!backupPicker.ok) throw new Error(selection.detail || 'Could not choose a backup file');
      if (selection.cancelled) return;
      const response = await fetch('/api/projects/backup/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({ directory: backupDirectory, backup_path: selection.path, passphrase: backupPassphrase }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not restore the encrypted backup');
      const restoredDocuments = (data.documents || []).map((document) => ({
        id: document.id,
        versionId: document.versionId,
        versions: document.versions || [],
        selectedVersionId: document.versionId,
        name: document.name,
        type: document.type,
        status: 'Restored project document',
        content: ['Loading a local parsed preview…'],
        rawText: '',
        heading: `${document.name} · restored project`,
        isProjectDocument: true,
      }));
      rememberProject(data, backupDirectory);
      setCurrentProject(data);
      setProjectDirectory(backupDirectory);
      setFiles(restoredDocuments);
      setActiveName(restoredDocuments[0]?.id || '');
      rememberActiveDocument(restoredDocuments[0]?.id);
      setSectionIndex(0);
      setBackupDialog(null);
      setBackupPassphrase('');
      if (restoredDocuments[0]) await loadProjectDocumentPreview(restoredDocuments[0].id, restoredDocuments[0].versionId, 'original', backupDirectory);
      setToast(`Restored encrypted project “${data.name}” to this computer`);
    } catch (error) {
      setToast(error.message || 'Could not restore the encrypted backup');
    } finally {
      setBackupBusy(false);
    }
  };

  const chooseProjectDirectory = async () => {
    if (!localToken) {
      setToast('Run npm run dev:app to start the local services before choosing a folder');
      return;
    }
    setProjectBusy(true);
    try {
      const response = await fetch('/api/dialogs/project-folder', {
        headers: { 'X-Local-App-Token': localToken },
        cache: 'no-store',
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not open the native folder picker');
      if (!data.cancelled) setProjectDirectory(data.directory);
    } catch (error) {
      setToast(error.message || 'Could not choose a project folder');
    } finally {
      setProjectBusy(false);
    }
  };

  const submitProject = async (event) => {
    event.preventDefault();
    if (!localToken) {
      setToast('The local service is unavailable. No project was changed.');
      return;
    }
    setProjectBusy(true);
    try {
      const response = await fetch(projectAction === 'create' ? '/api/projects' : '/api/projects/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify(projectAction === 'create'
          ? { name: projectName, directory: projectDirectory }
          : { directory: projectDirectory }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not open this project');
      const savedDocuments = (data.documents || []).map((document) => ({
        id: document.id,
        versionId: document.versionId,
        versions: document.versions || [],
        selectedVersionId: document.versionId,
        name: document.name,
        type: document.type,
        status: 'Saved original · v01',
        content: ['Loading a local parsed preview…'],
        rawText: '',
        heading: `${document.name} · saved original`,
        isProjectDocument: true,
      }));
      rememberProject(data, projectDirectory);
      setCurrentProject(data);
      setFiles(savedDocuments);
      setActiveName(savedDocuments[0]?.id || '');
      rememberActiveDocument(savedDocuments[0]?.id);
      setSectionIndex(0);
      setDenseText(false);
      setProjectModalOpen(false);
      setExportPreview(null);
      if (savedDocuments[0]) await loadProjectDocumentPreview(savedDocuments[0].id);
      setToast(`${projectAction === 'create' ? 'Created' : 'Opened'} local project “${data.name}”`);
    } catch (error) {
      setToast(error.message || 'Could not open this project');
    } finally {
      setProjectBusy(false);
    }
  };

  const rememberProject = (project, directory) => {
    if (!directory) return;
    const recent = {
      id: project.id || directory,
      name: project.name || 'Local workspace',
      directory,
    };
    const updated = [recent, ...recentProjects.filter((item) => item.directory !== directory)];
    setRecentProjects(updated);
    try {
      localStorage.setItem(RECENT_PROJECTS_KEY, JSON.stringify(updated));
      localStorage.setItem(ACTIVE_PROJECT_KEY, directory);
    } catch { /* Recent workspace shortcuts are optional when browser storage is unavailable. */ }
  };

  const openRecentProject = async (recent) => {
    if (!localToken || projectBusy) return;
    setProjectBusy(true);
    setProjectDirectory(recent.directory);
    try {
      const response = await fetch('/api/projects/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({ directory: recent.directory }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not open this workspace');
      const savedDocuments = (data.documents || []).map((document) => ({
        id: document.id,
        versionId: document.versionId,
        versions: document.versions || [],
        selectedVersionId: document.versionId,
        name: document.name,
        type: document.type,
        status: 'Saved original · v01',
        content: ['Loading a local parsed preview…'],
        rawText: '',
        heading: `${document.name} · saved original`,
        isProjectDocument: true,
      }));
      const activeDocument = savedDocuments.find((document) => document.id === readActiveDocumentId())
        || savedDocuments[0];
      rememberProject(data, recent.directory);
      setCurrentProject(data);
      setFiles(savedDocuments);
      setActiveName(activeDocument?.id || '');
      setSectionIndex(0);
      setDenseText(false);
      setView('preview');
      setUndo(null);
      setExportPreview(null);
      setRestorePreviewData(null);
      if (activeDocument) {
        rememberActiveDocument(activeDocument.id);
        await loadProjectDocumentPreview(activeDocument.id, activeDocument.versionId, 'original', recent.directory);
      }
      setToast(`Opened local workspace “${data.name}”`);
    } catch (error) {
      setToast(error.message || 'Could not open this workspace');
    } finally {
      setProjectBusy(false);
    }
  };

  useEffect(() => {
    if (!localToken || currentProject || projectBusy || activeProjectRestoreAttempted.current) return;
    let directory = '';
    try {
      directory = localStorage.getItem(ACTIVE_PROJECT_KEY) || recentProjects[0]?.directory || '';
    } catch {
      return;
    }
    if (!directory) return;
    activeProjectRestoreAttempted.current = true;
    const recent = recentProjects.find((project) => project.directory === directory)
      || { directory, name: 'Local workspace' };
    void openRecentProject(recent);
  }, [localToken]);

  const showProjectDialog = () => {
    setProjectAction('create');
    setProjectName('');
    setProjectDirectory('');
    setProjectModalOpen(true);
  };

  const undoDecision = async () => {
    if (!undo) return;
    if (undo.isCandidate) {
      try {
        const response = await fetch('/api/projects/candidate-decision', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
          body: JSON.stringify({
            directory: projectDirectory,
            document_id: activeFile.id,
            candidate_id: undo.candidateId,
            decision: undo.decision,
          }),
        });
        const updated = await response.json();
        if (!response.ok) throw new Error(updated.detail || 'Could not undo this decision');
        setExportPreview(null);
        if (candidateGroups.some((group) => group.candidateIds.includes(undo.candidateId))) {
          await loadProjectDocumentCandidates(activeFile.id);
        } else {
          setFiles((current) => current.map((file) => (
            file.id === activeFile.id
              ? { ...file, candidates: upsertCandidate(file.candidates || [], updated) }
              : file
          )));
        }
        setToast('Last candidate decision undone');
        setUndo(null);
      } catch (error) {
        setToast(error.message || 'Could not undo this decision');
      }
      return;
    }
    setDecisionSets((current) => ({ ...current, [activeName]: { ...(current[activeName] || { alex: 'suggested', cedar: 'suggested' }), [undo.groupId]: undo.decision } }));
    setConfirmationSets((current) => ({ ...current, [activeName]: { ...(current[activeName] || { alex: true, cedar: false }), [undo.groupId]: undo.confirmed } }));
    setToast('Last group decision undone');
    setUndo(null);
  };

  const switchFile = (file) => {
    setExportPreview(null);
    setActiveName(file.id || file.name);
    if (file.isProjectDocument) rememberActiveDocument(file.id);
    setSectionIndex(0);
    setDenseText(false);
    if (file.previewSections?.length) {
      setFiles((current) => current.map((item) => (
        item.id === file.id ? { ...item, content: file.previewSections[0].content } : item
      )));
    }
    setSelectedGroup('alex');
    setView('preview');
    setUndo(null);
    setMergeGroupIds([]);
    setSelectedCandidateIds([]);
    setBulkSelectedCandidateIds([]);
    setManualPhrase('');
    setToast(`Opened ${file.name} · original remains unchanged`);
    const selectedVersion = file.versions?.find((version) => version.id === file.selectedVersionId);
    if (file.isProjectDocument && !file.previewLoaded && !file.previewLoading) {
      void loadProjectDocumentPreview(file.id, file.selectedVersionId, selectedVersion?.kind);
    }
    if (file.isProjectDocument && (selectedVersion?.kind || 'original') === 'original' && file.previewLoaded && !file.candidateLoaded && !file.candidateLoading) {
      void loadProjectDocumentCandidates(file.id);
    }
  };

  return (
    <div className="app-shell" data-project-preview={activeFile.isProjectDocument ? 'true' : undefined} data-preview-error={activeFile.previewError ? 'true' : undefined} data-dense-text={denseText ? 'true' : undefined} data-docx-page-view={view === 'preview' && activeFile.isProjectDocument && activeFile.previewFormat === 'DOCX' ? 'true' : undefined}>
      <aside className="sidebar" data-od-id="sidebar">
        <div className="brand-row"><div className="brand-mark" aria-hidden="true">B</div><div><div className="brand-name">Blot</div><div className="brand-sub">private document workspace</div></div></div>
        <div className="side-section"><div className="side-label">Workspace</div><button className="side-link active" onClick={() => setToast('Review queue opened')}><span className="side-icon">◈</span> Review queue</button><button className="side-link" onClick={() => setToast('Showing all project files')}><span className="side-icon">□</span> All files <span style={{ marginLeft: 'auto', fontSize: 11 }}>{files.length}</span></button><button className="side-link" onClick={() => setToast('Activity is up to date')}><span className="side-icon">↺</span> Activity</button></div>
        <div className="side-section"><div className="side-label">Recent workspaces</div>{recentProjects.map((project) => <button key={project.directory} className={`side-link ${currentProject && project.directory === projectDirectory ? 'active' : ''}`} title={project.directory} disabled={!serviceAvailable || projectBusy} onClick={() => openRecentProject(project)}><span className="side-icon">▣</span><span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{project.name}</span></button>)}{recentProjects.length === 0 && <p style={{ margin: '0 10px 8px', color: 'color-mix(in oklch, var(--accent-on) 58%, transparent)', fontSize: 12 }}>Your saved workspaces will appear here.</p>}<button className="side-link" onClick={showProjectDialog}><span className="side-icon">＋</span> Add or open workspace</button></div>
        <div className="side-spacer" /><div className="local-badge"><span className="status-dot" style={{ background: serviceAvailable ? 'var(--success)' : 'var(--warn)' }} /><span><strong style={{ color: 'var(--accent-on)' }}>{serviceAvailable ? 'Local service ready' : 'UI preview mode'}</strong><br />{serviceAvailable ? 'Document content stays on this computer.' : 'Run npm run dev:app for local projects.'}</span></div>
      </aside>
      <main className="main">
        <header className="topbar"><div className="crumbs"><span>{currentProject?.name || 'Cedar briefing · demo'}</span><span>/</span><strong>{projectHasNoDocuments ? 'No documents' : activeFile.name}</strong></div><div className="top-actions"><button className="text-btn" onClick={() => { setBackupDialog(currentProject ? 'create' : 'restore'); setBackupPassphrase(''); }}>Encrypted backup</button><button className="icon-btn" type="button" aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`} aria-pressed={theme === 'dark'} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? '☀' : '☾'}</button><button className="icon-btn" aria-label="Open local settings" onClick={() => setToast('Local settings are available in the service configuration')}>•••</button><button className="primary-btn" disabled={currentProject && projectBusy} onClick={() => currentProject ? importProjectDocuments() : setModalOpen(true)}>{projectBusy ? 'Working…' : 'Import file'}</button></div></header>
        <div className="workspace" data-empty-project={projectHasNoDocuments ? 'true' : undefined}>
          {projectHasNoDocuments && <section className="panel empty-project-message"><span className="eyebrow">LOCAL PROJECT · READY</span><h1>Add a document to begin</h1><p>Choose a local document to review its supported editable text. Originals stay unchanged; exports are saved as separate project versions.</p><button className="primary-btn" type="button" onClick={importProjectDocuments} disabled={projectBusy}>{projectBusy ? 'Working…' : 'Import documents'}</button></section>}
           <div className="page-head"><div><p className="eyebrow">{activeFile.isProjectDocument ? (activeFile.previewError ? 'Saved original · preview unavailable' : `${activeVersion.kind === 'original' ? 'Original' : activeVersion.kind === 'restored' ? 'Restored version' : 'Obfuscated version'} · local parsed preview`) : 'Document review · version 03'}</p><h1>{activeFile.isProjectDocument ? (activeFile.previewError ? 'Preview unavailable' : 'Document review') : 'Prepare a safe copy'}</h1><p className="subhead">{activeFile.isProjectDocument ? (activeFile.previewError ? 'This saved version could not be parsed. The original remains unchanged.' : 'Review candidate decisions in supported editable text. Project versions remain local.') : officeFileNeedsProject ? 'Create or open a local project, then import the original Office file to preserve its format in the obfuscated copy.' : 'Review suggested terms before this editable-text document leaves your computer. Similarity is a prompt, never a decision.'}</p></div><button className="primary-btn" disabled={!canStartExport || exportBusy} onClick={exportCopy} title={officeFileNeedsProject ? 'Import the original Office file into a local project to enable format-preserving export.' : undefined}>{exportBusy ? 'Preparing…' : 'Preview & export obfuscated copy'}</button></div>
          <section className="layout">
            <aside className="panel file-panel"><div className="panel-head"><span className="panel-title">{currentProject ? 'Project documents' : 'Project files'}</span><span className="panel-meta">{currentProject ? `${projectDocuments.length} saved` : `${files.length} items`}</span></div><div className="file-list">{(currentProject ? projectDocuments : files).map((file) => <button key={file.id || file.name} className={`file-item ${(file.id || file.name) === activeName ? 'active' : ''}`} onClick={() => switchFile(file)}><span className="file-type">{file.type}</span><span className="file-copy"><span className="file-name">{file.name}</span><span className="file-status">{file.status}</span></span><span className="file-check">{(file.id || file.name) === activeName ? '●' : file.status.includes('Ready') ? '✓' : ''}</span></button>)}{currentProject && projectDocuments.length === 0 && <p className="empty-file-list">No project documents yet.</p>}</div></aside>
            <section className="panel review-panel">
               <div className="review-toolbar"><div className="review-title"><strong>{activeFile.name}</strong><span>{activeFile.isProjectDocument ? `${activeVersion.kind === 'original' ? 'Saved original' : activeVersion.kind === 'restored' ? 'Restored copy' : 'Obfuscated copy'} · local preview` : `Editable text preview · local${activeFile.type === 'PPTX' ? ` · ${activeFile.content.length} slides` : ''}`}</span></div><div className="review-toolbar-actions">{activeFile.isProjectDocument && <label className="version-select">Version<select aria-label="Select document version" value={activeFile.selectedVersionId || activeFile.versionId} onChange={(event) => selectProjectVersion(event.target.value)}>{(activeFile.versions || []).map((version) => <option key={version.id} value={version.id}>{version.kind === 'original' ? 'Original' : version.kind === 'restored' ? 'Restored' : 'Obfuscated'} · {version.name}</option>)}</select></label>}<div className="view-switch" role="group" aria-label="Document view"><button type="button" className={view === 'preview' ? 'active' : ''} aria-pressed={view === 'preview'} onClick={() => setView('preview')}>Preview</button><button type="button" className={view === 'changes' ? 'active' : ''} aria-pressed={view === 'changes'} onClick={() => setView('changes')}>Changes <span>{matchCount}</span></button></div></div></div>
                 {reviewableProjectDocument && <div className="priority-area"><fieldset className="priority-fieldset"><legend>Obfuscation level</legend><div className="priority-scale" aria-hidden="true"><span>1 · 0%</span><strong>Level {level}</strong><span>10 · 100%</span></div><input className="priority-slider" type="range" min="1" max="10" step="1" value={level} aria-label="Obfuscation level" aria-valuetext={`Level ${level}: ${level === 1 ? '0% obfuscation; no terms selected' : level === 10 ? '100% obfuscation; priorities 2 through 10 selected' : `priorities 2 through ${level} selected`}`} onChange={(event) => { setExportPreview(null); setBulkSelectedCandidateIds([]); setLevel(Number(event.target.value)); }} /><p className="priority-help">Priorities run from 2 (most sensitive) to 10 (least sensitive). Level 1 selects none; level 10 selects priorities 2–10.</p></fieldset></div>}
                {reviewableProjectDocument && <p className="sensitivity-note">Include and Exclude decisions apply only to candidates selected at this level.</p>}
                {view === 'preview' && <p className="term-interaction-help">Click a highlighted word to include · double-click to exclude · right-click for Reset / Include / Exclude.</p>}
                {view === 'preview' && activeFile.isProjectDocument && activeFile.previewFormat === 'DOCX' && <div className="preview page-preview-shell">
                  <div className="preview-note"><span className="status-dot" /><span>Word-compatible page layout · highlighted candidates match the review list · original document remains unchanged.</span></div>
                  {activeFile.pagePreviewPageCount > 0 && <div className="docx-page-navigation" role="group" aria-label="DOCX page navigation">
                    <button className="small-btn" type="button" onClick={() => selectDocxPage((activeFile.pagePreviewPage || 1) - 1)} disabled={(activeFile.pagePreviewPage || 1) <= 1}>← Previous page</button>
                    <span aria-live="polite">Page <strong>{activeFile.pagePreviewPage || 1}</strong> of <strong>{activeFile.pagePreviewPageCount}</strong></span>
                    <button className="small-btn" type="button" onClick={() => selectDocxPage((activeFile.pagePreviewPage || 1) + 1)} disabled={(activeFile.pagePreviewPage || 1) >= activeFile.pagePreviewPageCount}>Next page →</button>
                  </div>}
                  <div className="docx-page-canvas-wrapper">
                    {pagePreviewLoading && <p role="status">Preparing page preview…</p>}
                    {pagePreviewError && <p className="docx-page-error" role="status">{pagePreviewError} <button className="small-btn" type="button" onClick={() => loadProjectDocumentPreview(activeFile.id, activeFile.selectedVersionId, activeVersion.kind)}>Retry page rendering</button></p>}
                    {!pagePreviewLoading && !pagePreviewError && pagePreviewDocument?.url === activeFile.pagePreviewUrl && <div ref={pagePreviewFrame} className="docx-rendered-page" onClick={handlePagePreviewClick} onDoubleClick={handlePagePreviewDoubleClick} onContextMenu={handlePagePreviewContextMenu}>
                      <canvas ref={pagePreviewCanvas} className="docx-page-canvas" aria-label={`Page ${activeFile.pagePreviewPage || 1}`} />
                      <div ref={pagePreviewTextLayer} className="textLayer docx-text-layer" aria-label="Selectable document text" />
                      <div ref={pagePreviewHighlightLayer} className="docx-page-highlight-layer" aria-hidden="false">{pagePreviewHighlights.map((highlight, index) => <button key={`${highlight.candidate.id}-${highlight.matchIndex}-${highlight.fragmentIndex}`} type="button" data-page-term-index={index} className={`docx-term-highlight ${highlight.candidate.decision === 'excluded' ? 'excluded' : highlight.candidate.decision === 'included' ? 'included' : 'auto'}`} style={{ left: highlight.left, top: highlight.top, width: highlight.width, height: highlight.height }} tabIndex={highlight.fragmentIndex === 0 ? 0 : -1} aria-hidden={highlight.fragmentIndex !== 0 ? 'true' : undefined} aria-label={highlight.fragmentIndex === 0 ? `${highlight.candidate.term}: ${highlight.candidate.decision}; click to include, double-click to exclude, or right-click for Reset, Include, or Exclude` : undefined} title={`${highlight.candidate.term} · ${highlight.candidate.decision}`} />)}</div>
                    </div>}
                  </div>
                </div>}
                {view === 'preview' && previewSections.length > 1 && <div className="preview-navigation preview-navigation-main" role="group" aria-label="Preview section navigation" aria-describedby="preview-page-navigation-help" aria-keyshortcuts="ArrowLeft ArrowRight Home End" onKeyDown={handlePreviewNavigationKeyDown}>
                 <span className="sr-only" id="preview-page-navigation-help">Use Left or Right Arrow to move between preview sections, or Home and End to jump to the first and last sections.</span>
                 <button className="small-btn" type="button" onClick={() => selectPreviewSection(sectionIndex - 1)} disabled={sectionIndex <= 0} aria-label="Previous preview section">← Previous</button>
                 <span aria-live="polite"><strong>Section {sectionIndex + 1} of {previewSections.length}</strong><small>{activeSection?.label}</small></span>
                 <button className="small-btn" type="button" onClick={() => selectPreviewSection(sectionIndex + 1)} disabled={sectionIndex >= previewSections.length - 1} aria-label="Next preview section">Next →</button>
               </div>}
              {view === 'preview' ? <div className="preview"><div className="preview-note"><span className="status-dot" /><span>{activeFile.isProjectDocument ? (activeFile.candidateLoading ? 'Scanning supported editable text locally…' : `${visibleCandidates.length} candidates shown at level ${level}. Review decisions below; only supported editable text is scanned.`) : `${visibleGroups.length} suggested groups are visible at this level. Click a highlighted term to decide.`}</span></div><article className="doc-page"><div className="doc-kicker">BOARD UPDATE · 04 OCTOBER 2026</div><h2>{activeFile.heading}</h2>{activeFile.content.map((paragraph, index) => <p key={`${activeFile.name}-${index}`}>{renderParagraph(paragraph)}</p>)}<div className="legend"><span className="legend-item"><span className="legend-swatch" />Suggested</span><span className="legend-item"><span className="legend-swatch manual" />Manual decision</span><span className="legend-item">Click a term to inspect its group</span></div></article><div className="preview-foot"><span><strong>{activeFile.isProjectDocument ? visibleCandidates.reduce((sum, candidate) => sum + (candidate.decision === 'excluded' ? 0 : candidate.occurrenceCount), 0) : matchCount}</strong> included or suggested occurrences at level <strong>{level}</strong></span><span>Original stays unchanged</span></div>{undo?.file === activeName && <button className="small-btn undo-button" onClick={undoDecision}>Undo last decision</button>}</div> : <div className="preview changes-pane"><div className="preview-note"><span className="status-dot" /><span>Export diff for version 03</span></div><table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}><thead><tr style={{ color: 'var(--muted)', font: '11px var(--font-mono)', textAlign: 'left' }}><th style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>OCCURRENCE</th><th style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>REPLACEMENT</th><th style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>DECISION</th></tr></thead><tbody>{changeRows.map(({ group, occurrences, decision }) => <tr key={group.id}><td style={{ padding: '12px 8px', borderBottom: '1px solid var(--border-soft)' }}>{group.term} · {occurrences} {occurrences === 1 ? 'match' : 'matches'}</td><td style={{ padding: '12px 8px', borderBottom: '1px solid var(--border-soft)', fontFamily: 'var(--font-mono)', color: 'var(--accent)' }}>[[{group.token}]]</td><td style={{ padding: '12px 8px', borderBottom: '1px solid var(--border-soft)' }}>{decision === 'excluded' ? 'Excluded' : decision === 'included' ? 'Included' : 'Suggested'}</td></tr>)}</tbody></table></div>}
            </section>
            <aside className="right-stack">
                {reviewableProjectDocument ? <section className="panel candidate-panel">
                <div className="panel-head"><span className="panel-title">Word review</span><div className="ollaya-csv-heading"><span className="panel-meta">{includedCandidates.length} included words</span><button className="small-btn" type="button" title={`Open ${ollayaCsvFilename || `${activeFile.name} Ollaya CSV`}`} aria-label={`Open Ollaya CSV for ${activeFile.name}`} onClick={() => { setOllayaCsvOpen(true); void reloadOllayaCsv(activeFile.id, projectDirectory, { merge: false }); }}>CSV · {activeFile.name}</button></div></div>
                <label className="dense-toggle"><input type="checkbox" checked={denseText} onChange={(event) => setDenseText(event.target.checked)} /> Dense text view</label>
                {previewSections.length > 1 && <div className="preview-navigation" role="group" aria-label="Preview section navigation" aria-describedby="preview-navigation-help" aria-keyshortcuts="ArrowLeft ArrowRight Home End" onKeyDown={handlePreviewNavigationKeyDown}>
                  <span className="sr-only" id="preview-navigation-help">Use Left or Right Arrow to move between sections, or Home and End to jump to the first and last sections.</span>
                  <button className="small-btn" type="button" onClick={() => selectPreviewSection(sectionIndex - 1)} disabled={sectionIndex <= 0} aria-label="Previous preview section">Previous</button>
                  <span aria-live="polite"><strong>Section {sectionIndex + 1} of {previewSections.length}</strong><small>{activeSection?.label}</small></span>
                  <button className="small-btn" type="button" onClick={() => selectPreviewSection(sectionIndex + 1)} disabled={sectionIndex >= previewSections.length - 1} aria-label="Next preview section">Next</button>
                </div>}
                <form className="manual-candidate-form" onSubmit={addManualCandidate}>
                  <label htmlFor="manual-candidate">Select a phrase in the preview or type it</label>
                  <div><input id="manual-candidate" value={manualPhrase} onChange={(event) => setManualPhrase(event.target.value)} maxLength={256} placeholder="Type a phrase to include" /><button className="small-btn primary" type="submit" disabled={activeFile.candidateLoading || !manualPhrase.trim()}>Add</button></div>
                </form>
                <div className="candidate-counts" role="status" aria-live="polite" aria-label="Candidate review counts">
                  <span><strong>{candidateDecisionCounts.suggested}</strong> need review</span>
                  <span><strong>{candidateDecisionCounts.included}</strong> included</span>
                  <span><strong>{candidateDecisionCounts.excluded}</strong> excluded</span>
                  {notIncludedByLevelCount > 0 && <span><strong>{notIncludedByLevelCount}</strong> not selected at this level</span>}
                </div>
                <div className="candidate-bulk-panel" aria-label="Bulk candidate decisions">
                  <div className="candidate-subhead">Bulk decisions · {reviewCandidates.length} reviewable words</div>
                  <div className="candidate-bulk-actions">
                    <button className="small-btn primary" type="button" disabled={activeFile.candidateLoading || candidateBulkBusy || !reviewCandidates.length} onClick={() => applyBulkCandidateDecision(reviewCandidates.map((candidate) => candidate.id), 'included')}>Include all words</button>
                    <button className="small-btn" type="button" disabled={activeFile.candidateLoading || candidateBulkBusy || !reviewCandidates.length} onClick={() => applyBulkCandidateDecision(reviewCandidates.map((candidate) => candidate.id), 'excluded')}>Exclude all words</button>
                  </div>
                  <p>Automatically excluded words are hidden from this review list. A common-word Yes with No on both identifier and operational signals is excluded; either affirmative signal includes the word.</p>
                  {[
                    ['isIdentifier', 'Identifier / named entity'],
                    ['isOperationallySignificant', 'Operational significance'],
                  ].map(([signalName, label]) => {
                    const matchingCount = getCandidatesMatchingSignal(reviewCandidates, signalName, signalThresholds[signalName]).length;
                    return <div className="candidate-signal-select" key={signalName}>
                      <label htmlFor={`threshold-${signalName}`}>{label} · Yes at least</label>
                      <input id={`threshold-${signalName}`} type="range" min="50" max="100" step="5" value={signalThresholds[signalName]} aria-label={`${label} yes-confidence threshold percentage`} aria-valuetext={`${signalThresholds[signalName]} percent yes`} onChange={(event) => setSignalThresholds((current) => ({ ...current, [signalName]: Number(event.target.value) }))} />
                      <output htmlFor={`threshold-${signalName}`}>{signalThresholds[signalName]}%</output>
                      <button className="small-btn" type="button" disabled={activeFile.candidateLoading || candidateBulkBusy || matchingCount === 0} onClick={() => selectCandidatesBySignal(signalName)}>Add {matchingCount}</button>
                    </div>;
                  })}
                  <div className="candidate-bulk-selected">
                    <span>{bulkSelectedCandidateIds.length} selected</span>
                    <button className="small-btn" type="button" disabled={!bulkSelectedCandidateIds.length || candidateBulkBusy} onClick={() => setBulkSelectedCandidateIds([])}>Clear</button>
                    <button className="small-btn primary" type="button" disabled={!bulkSelectedCandidateIds.length || candidateBulkBusy || activeFile.candidateLoading} onClick={() => applyBulkCandidateDecision(bulkSelectedCandidateIds, 'included')}>Include selected</button>
                    <button className="small-btn" type="button" disabled={!bulkSelectedCandidateIds.length || candidateBulkBusy || activeFile.candidateLoading} onClick={() => applyBulkCandidateDecision(bulkSelectedCandidateIds, 'excluded')}>Exclude selected</button>
                  </div>
                </div>
                <div className="candidate-subsection">
                  <div className="candidate-subhead">Manual groups</div>
                  <p>Select at least two ungrouped candidates to apply decisions to them together.</p>
                  <button className="small-btn" type="button" disabled={activeFile.candidateLoading || candidateBulkBusy || selectedCandidateIds.length < 2} onClick={() => applyCandidateGroupOperation('add', selectedCandidateIds)}>
                    Group selected candidates ({selectedCandidateIds.length})
                  </button>
                </div>
                <div className="graph-list">
                  {activeFile.candidateLoading && <p role="status">{activeFile.candidateProgress ? `Stage 1 complete · ${activeFile.candidateProgress} unique words found; Stage 2 · ${queriedCandidateCount} queried by Ollaya.` : 'Stage 1 · extracting unique words from supported text…'}</p>}
                  {activeFile.candidateError && <p className="candidate-error" role="alert">{activeFile.candidateError} <button className="small-btn" onClick={() => loadProjectDocumentCandidates(activeFile.id)}>Retry</button></p>}
                  {activeFile.candidateLimitReached && <p className="candidate-limit">Candidate list reached its display limit.</p>}
                  {activeFile.ollayaWarning && <p className="candidate-error" role="status">{activeFile.ollayaWarning}</p>}
                  {includedCandidates.map((candidate) => <div className="graph-card candidate-card" key={candidate.id}>
                    <label className="candidate-bulk-select"><input type="checkbox" aria-label={`Select ${candidate.term} for a bulk Include or Exclude decision`} checked={bulkSelectedCandidateIds.includes(candidate.id)} disabled={candidateBulkBusy} onChange={(event) => setBulkSelectedCandidateIds((current) => event.target.checked ? [...new Set([...current, candidate.id])] : current.filter((id) => id !== candidate.id))} />Select for Include / Exclude</label>
                    <label className="candidate-group-select"><input type="checkbox" aria-label={`Select ${candidate.term} for a manual group`} checked={selectedCandidateIds.includes(candidate.id)} disabled={activeFile.candidateLoading || candidateBulkBusy || groupedCandidateIds.has(candidate.id)} onChange={(event) => setSelectedCandidateIds((current) => event.target.checked ? [...current, candidate.id] : current.filter((id) => id !== candidate.id))} />Group with another candidate</label>
                    <div className="graph-card-head"><span className="graph-term">{candidate.term}</span><span className="confidence">{candidate.scoreStatus === 'queued' ? 'Waiting for Ollaya' : candidate.reviewPriority == null ? 'Review Priority —' : `Review Priority ${candidate.reviewPriority}/10`}</span></div>
                    <p className="graph-reason">Redaction confidence {candidate.redactionConfidence == null ? candidate.scoreStatus === 'complete' ? 'no affirmative signal' : 'unavailable' : `${Math.round(candidate.redactionConfidence * 100)}%`}{candidate.scoreStatus === 'complete' && candidate.redactionConfidence == null ? ' · not auto-suggested' : ''} · {candidate.nerLabels?.length ? `NER · ${candidate.nerLabels.join(', ')} · model score ${Math.round((candidate.nerScore || 0) * 100)}%${candidate.source === 'manual' ? ' · manual' : ''}` : `${candidate.category.replaceAll('_', ' ').toLowerCase()} · ${candidate.source}`} · {candidate.occurrenceCount} {candidate.occurrenceCount === 1 ? 'occurrence' : 'occurrences'}</p>
                    {candidate.scoreStatus !== 'queued' && <p className="candidate-location">Ollaya {candidate.scoringModel || 'unavailable'} · identifier: {candidate.signals?.isIdentifier ? `${candidate.signals.isIdentifier.answer} (${Math.round(candidate.signals.isIdentifier.probabilityYes * 100)}% yes)` : 'unavailable'} · operational significance: {candidate.signals?.isOperationallySignificant ? `${candidate.signals.isOperationallySignificant.answer} (${Math.round(candidate.signals.isOperationallySignificant.probabilityYes * 100)}% yes)` : 'unavailable'} · common word: {candidate.signals?.isCommonWord ? `${candidate.signals.isCommonWord.answer} (${Math.round(candidate.signals.isCommonWord.probabilityYes * 100)}% yes)` : 'unavailable'}{candidate.commonWordOverride ? ' · common-word signal overridden by significant signal' : ''}</p>}
                    {candidate.occurrences[0] && <p className="candidate-location">{candidate.occurrences[0].location}{candidate.occurrencesTruncated ? ' · locations truncated' : ''}</p>}
                    <div className="graph-actions">
                      <button className={`small-btn ${candidate.decision === 'included' ? 'primary' : ''}`} disabled={activeFile.candidateLoading || candidateBulkBusy} onClick={() => setProjectCandidateDecision(candidate, candidate.decision === 'included' ? 'suggested' : 'included')}>{candidate.decision === 'included' ? 'Included' : 'Include'}</button>
                      <button className={`small-btn ${candidate.decision === 'excluded' ? 'selected' : ''}`} disabled={activeFile.candidateLoading || candidateBulkBusy} onClick={() => setProjectCandidateDecision(candidate, candidate.decision === 'excluded' ? 'suggested' : 'excluded')}>{candidate.decision === 'excluded' ? 'Excluded' : 'Exclude'}</button>
                    </div>
                  </div>)}
                  {!activeFile.candidateLoading && !activeFile.candidateError && includedCandidates.length === 0 && <p style={{ padding: 10, color: 'var(--muted)', fontSize: 12 }}>{activeFile.previewError ? 'Word review requires a readable local preview.' : reviewCandidates.length ? 'No words are included yet.' : candidates.length ? 'All extracted words were automatically excluded.' : 'No words found in supported editable text.'}</p>}
                </div>
                {candidateGroups.length > 0 && <div className="candidate-subsection"><div className="candidate-subhead">Confirmed groups</div>{candidateGroups.map((group) => <div className="confirmed-group" key={group.id}><label><input type="checkbox" checked={mergeGroupIds.includes(group.id)} onChange={(event) => setMergeGroupIds((current) => event.target.checked ? [...current, group.id] : current.filter((id) => id !== group.id))} /> Merge group</label>{group.candidateIds.map((candidateId) => <div className="confirmed-member" key={candidateId}><span>{candidatesById[candidateId]?.term || 'Candidate'}</span><div>{group.candidateIds.length > 1 && <button className="small-btn" onClick={() => applyCandidateGroupOperation('split', [candidateId], group.id)}>Split out</button>}<button className="small-btn" onClick={() => applyCandidateGroupOperation('remove', [candidateId], group.id)}>Remove</button></div></div>)}</div>)}<button className="small-btn" disabled={mergeGroupIds.length < 2} onClick={() => applyCandidateGroupOperation('merge', [], undefined, mergeGroupIds)}>Merge selected groups</button></div>}
              </section> : <section className="panel"><div className="panel-head"><span className="panel-title">{activeFile.isProjectDocument ? 'Exported version' : 'Suggested groups'}</span><span className="panel-meta">{activeFile.isProjectDocument ? 'Read only' : `${visibleGroups.length} groups`}</span></div><div className="graph-list">{activeFile.isProjectDocument ? <><p style={{ padding: 10, color: 'var(--muted)', fontSize: 12 }}>This obfuscated version is read-only. Select Original above to review or adjust its candidate decisions.</p><button className="small-btn" type="button" onClick={() => downloadProjectVersion(activeVersion.id, activeVersion.name)}>Download this version</button><label className="dense-toggle"><input type="checkbox" checked={denseText} onChange={(event) => setDenseText(event.target.checked)} /> Dense text view</label>{previewSections.length > 1 && <div className="preview-navigation" role="group" aria-label="Preview section navigation" aria-describedby="preview-navigation-help" aria-keyshortcuts="ArrowLeft ArrowRight Home End" onKeyDown={handlePreviewNavigationKeyDown}><span className="sr-only" id="preview-navigation-help">Use Left or Right Arrow to move between sections, or Home and End to jump to the first and last sections.</span><button className="small-btn" type="button" onClick={() => selectPreviewSection(sectionIndex - 1)} disabled={sectionIndex <= 0} aria-label="Previous preview section">Previous</button><span aria-live="polite"><strong>Section {sectionIndex + 1} of {previewSections.length}</strong><small>{activeSection?.label}</small></span><button className="small-btn" type="button" onClick={() => selectPreviewSection(sectionIndex + 1)} disabled={sectionIndex >= previewSections.length - 1} aria-label="Next preview section">Next</button></div>}</> : visibleGroups.length ? visibleGroups.map((group) => <div key={group.id} className={`graph-card ${selectedGroup === group.id ? 'selected' : ''}`} onClick={() => setSelectedGroup(group.id)}><div className="graph-card-head"><span className="graph-term">{group.term}</span><span className="confidence">{group.confidence} match</span></div><p className="graph-reason">{group.reason}</p><div className="member-row">{group.members.map((member, index) => <span key={member} className={`member ${confirmed[group.id] || (index === 0 && group.id === 'alex') ? 'confirmed' : ''}`}>{member} · {countMatches(activeText, member)}</span>)}</div><div className="graph-actions"><button className="small-btn primary" onClick={(event) => { event.stopPropagation(); changeDecision(group.id, 'included'); }}>{decisions[group.id] === 'included' ? 'Included' : 'Include group'}</button><button className="small-btn" onClick={(event) => { event.stopPropagation(); changeDecision(group.id, 'excluded'); }}>Exclude</button></div></div>) : <p style={{ padding: 10, color: 'var(--muted)', fontSize: 12 }}>No detected groups at this level.</p>}</div></section>}
              {activeFile.isProjectDocument && <section className="panel coverage-panel" aria-labelledby="coverage-title">
                <div className="panel-head"><span id="coverage-title" className="panel-title">Coverage and limits</span><span className="panel-meta">{activeFile.previewFormat || activeFile.type}</span></div>
                <div className={`coverage-alert ${unsupportedPartCount || previewWarnings.length ? 'has-warning' : ''}`} role={unsupportedPartCount || previewWarnings.length ? 'alert' : 'status'}>
                  {previewWarnings.length > 0 ? previewWarnings.join(' ') : unsupportedPartCount > 0 ? `${unsupportedPartCount} unsupported package parts were detected.` : 'This report describes adapter coverage; it does not guarantee that every sensitive value was detected.'}
                </div>
                <dl className="coverage-stats">
                  <div><dt>Adapter / encoding</dt><dd>{activeFile.previewFormat || 'Loading'}{activeFile.previewEncoding ? ` · ${activeFile.previewEncoding}` : ''}</dd></div>
                  {activeFile.previewLineEndings && <div><dt>Line endings</dt><dd>{activeFile.previewLineEndings}</dd></div>}
                  {activeFile.previewDialect && <div><dt>CSV delimiter</dt><dd>{JSON.stringify(activeFile.previewDialect.delimiter)}</dd></div>}
                  {previewCoverage ? <>
                    <div><dt>XML parts examined</dt><dd>{previewCoverage.examinedXmlPartCount}</dd></div>
                    <div><dt>Text-bearing parts</dt><dd>{previewCoverage.textPartCount}</dd></div>
                    <div><dt>Non-XML parts skipped</dt><dd>{previewCoverage.skippedPartCount}</dd></div>
                    <div><dt>Unsupported parts detected</dt><dd>{previewCoverage.unsupportedPartCount}</dd></div>
                  </> : <div><dt>Coverage inventory</dt><dd>Package-part inventory not provided for this format</dd></div>}
                  {activeFile.previewTruncated && <div><dt>Preview</dt><dd>Truncated; saved original remains complete</dd></div>}
                  {activeFile.previewSectionsTruncated && <div><dt>Sections</dt><dd>Section list or text truncated to preview limits</dd></div>}
                  {activeFile.existingPlaceholderLikeTextCount > 0 && <div><dt>Placeholder-like strings</dt><dd>{activeFile.existingPlaceholderLikeTextCount} found; review before processing</dd></div>}
                </dl>
                {previewCoverage && <details className="coverage-details">
                  <summary>View bounded package-part inventory</summary>
                  {previewCoverage.partNamesTruncated && <p className="coverage-truncated">Part names are truncated to a bounded list.</p>}
                  {[['Examined XML parts', previewCoverage.examinedXmlParts], ['Text-bearing parts', previewCoverage.textParts], ['Skipped parts', previewCoverage.skippedParts], ['Unsupported parts', previewCoverage.unsupportedParts]].map(([label, parts]) => parts?.length > 0 && <div className="coverage-part-list" key={label}><strong>{label}</strong><ul>{parts.map((part) => <li key={`${label}-${part}`}>{part}</li>)}</ul></div>)}
                </details>}
                <p className="coverage-scope">Only adapter-supported editable text is reviewed. Images/OCR, metadata, macros, embedded binary content, and unhandled text surfaces are not analyzed. Unsupported-part warnings must be considered before any later export.</p>
              </section>}
              <section className="panel status-card"><div className="status-line"><span className="status-dot" /><div><strong>Private local workspace</strong><p>{currentProject ? 'Project metadata and graph state are stored locally; demo documents are not part of this project.' : 'Text preview runs in this browser. No document is uploaded.'}</p></div></div><hr className="rule" /><div className="status-stat"><span>Graph protection</span><span>{currentProject ? 'AES-GCM' : 'Session only'}</span></div><div className="status-stat"><span>Project state</span><span>{currentProject ? 'saved locally' : 'demo only'}</span></div><div className="status-stat"><span>File ceiling</span><span>100 MB</span></div></section>
              {activeFile.isProjectDocument && <section className="panel status-card"><div className="panel-head" style={{ padding: '0 0 12px', border: 0, minHeight: 'auto' }}><span className="panel-title">Restore returned Office file</span><span className="panel-meta">Exact tokens only</span></div><p style={{ margin: '0 0 12px', color: 'var(--muted)', fontSize: 12, lineHeight: 1.45 }}>{activeVersion.kind === 'obfuscated' ? 'Choose the returned DOCX, PPTX, or XLSX associated with this obfuscated version. Unknown or changed tokens remain untouched.' : 'Select an obfuscated DOCX, PPTX, or XLSX version above to restore a returned file.'}</p><button className="primary-btn" style={{ width: '100%' }} disabled={activeVersion.kind !== 'obfuscated' || restoreBusy} onClick={previewProjectRestoration}>{restoreBusy ? 'Preparing…' : 'Choose file & preview'}</button>{activeFile.restoreReport && <dl className="restore-report"><div><dt>Exact occurrences restored</dt><dd>{activeFile.restoreReport.restoredCount}</dd></div><div><dt>Unresolved occurrences</dt><dd>{activeFile.restoreReport.unresolvedCount}</dd></div></dl>}</section>}
            </aside>
          </section>
        </div>
      </main>
      {ollayaCsvOpen && reviewableProjectDocument && <div className="modal-backdrop open ollaya-csv-backdrop">
        <section className="ollaya-csv-dialog" role="dialog" aria-modal="true" aria-labelledby="ollaya-csv-title">
          <header className="ollaya-csv-dialog-head"><div><h2 id="ollaya-csv-title">Ollaya results · {activeFile.name}</h2><code>{ollayaCsvFilename || 'Loading document CSV…'}</code></div><button className="icon-btn ollaya-csv-close" type="button" aria-label="Close CSV editor" onClick={closeOllayaCsv}>×</button></header>
          <div className="ollaya-csv-dialog-status" role="status" aria-live="polite">{ollayaCsvError || ollayaCsvStatus}{ollayaCsvDirty ? ' · Unsaved edits' : ''}</div>
          <div className="ollaya-csv-scroll ollaya-csv-dialog-scroll"><table className="ollaya-csv-table ollaya-csv-dialog-table" style={{ width: Object.values(ollayaColumnWidths).reduce((sum, width) => sum + width, 0) }}>
            <colgroup>{OLLAYA_CSV_COLUMNS.map(([column]) => <col key={column} style={{ width: ollayaColumnWidths[column] }} />)}</colgroup>
            <thead><tr>{OLLAYA_CSV_COLUMNS.map(([column]) => <th key={column} scope="col" style={{ width: ollayaColumnWidths[column] }}>
              <span>{column}</span><button className="ollaya-column-resizer" type="button" aria-label={`Resize ${column} column`} onPointerDown={(event) => startOllayaColumnResize(column, event)} onPointerMove={moveOllayaColumnResize} onPointerUp={endOllayaColumnResize} onPointerCancel={endOllayaColumnResize} />
            </th>)}</tr></thead>
            <tbody>{ollayaRows.map((row, rowIndex) => <tr key={`${row._documentId}-${row._originalWord || row.word}-${rowIndex}`}>
              {OLLAYA_CSV_COLUMNS.map(([column]) => <td key={column}>{column === 'is_correct'
                ? <select aria-label={`${row.word} is_correct`} value={row[column] || 'N'} disabled={ollayaCsvSaving || ollayaCacheClearing} onChange={(event) => editOllayaCsvCell(rowIndex, column, event.target.value)}><option value="N">N</option><option value="Y">Y</option></select>
                : <input aria-label={`${row.word} ${column}`} type={column.endsWith('_percent') ? 'number' : 'text'} min={column.endsWith('_percent') ? 0 : undefined} max={column.endsWith('_percent') ? 1 : undefined} step={column.endsWith('_percent') ? 0.01 : undefined} maxLength={column === 'word' ? 256 : undefined} value={row[column] ?? ''} disabled={ollayaCsvSaving || ollayaCacheClearing} onChange={(event) => editOllayaCsvCell(rowIndex, column, event.target.value)} />}</td>)}
            </tr>)}
            {!ollayaRows.length && <tr><td className="ollaya-csv-empty" colSpan={OLLAYA_CSV_COLUMNS.length}>{ollayaCsvError || (ollayaCsvStatus === 'Loading CSV…' ? ollayaCsvStatus : 'Results will appear as Ollaya scores this file.')}</td></tr>}
          </tbody></table></div>
          <p className="ollaya-csv-note">Probabilities use 0–1 values. Each source file has its own CSV. is_correct defaults to N.</p>
          <footer className="ollaya-csv-dialog-actions"><button className="text-btn" type="button" onClick={closeOllayaCsv}>Close</button><button className="text-btn" type="button" onClick={clearOllayaCache} disabled={ollayaCsvSaving || ollayaCacheClearing || activeFile.candidateLoading}>{ollayaCacheClearing ? 'Clearing & rescoring…' : 'Clear response cache'}</button><button className="primary-btn" type="button" onClick={saveOllayaCsv} disabled={ollayaCsvSaving || ollayaCacheClearing || !ollayaCsvDirty}>{ollayaCsvSaving ? 'Saving…' : 'Save'}</button></footer>
        </section>
      </div>}
      {exportPreview && <div className="modal-backdrop open" onMouseDown={(event) => { if (event.target === event.currentTarget && !exportBusy) setExportPreview(null); }}><section className="modal export-preview-modal" role="dialog" aria-modal="true" aria-labelledby="export-preview-title" aria-describedby="export-preview-description"><h2 id="export-preview-title">Review obfuscated copy</h2><p id="export-preview-description">{exportPreview.outputName} · {exportPreview.matchCount} supported-text occurrences will change. This preview does not modify the original.</p><div className="export-preview-content"><h3>Selected replacements</h3><ul className="export-match-list">{exportPreview.matches.map((match) => <li key={match.candidateId}><span><strong>{match.term}</strong> · {match.occurrenceCount} {match.occurrenceCount === 1 ? 'match' : 'matches'}</span><code>{match.token}</code></li>)}</ul><h3>Output preview · {exportPreview.format}</h3><pre>{exportPreviewText || 'No supported text is present in this preview.'}</pre><h3>Coverage and warnings</h3>{exportPreview.warnings.length ? <ul className="export-warning-list">{exportPreview.warnings.map((warning, index) => <li key={`${index}-${warning}`}>{warning}</li>)}</ul> : <p>No adapter warnings were reported. This is not a guarantee that all sensitive information was found.</p>}<p>Only adapter-supported editable text is processed. Images/OCR, metadata, macros, embedded binary content, and unhandled text surfaces are not sanitized. The private replacement map remains encrypted in this project and is not included in the output file.</p></div>{exportPreview.requiresAcknowledgement && <label className="export-warning-ack"><input type="checkbox" checked={exportAcknowledged} onChange={(event) => setExportAcknowledged(event.target.checked)} /><span>I reviewed the coverage and placeholder warnings and understand unsupported or unrecognized content may remain.</span></label>}<div className="modal-actions"><button className="text-btn" type="button" onClick={() => setExportPreview(null)} disabled={exportBusy}>Cancel</button><button className="primary-btn" type="button" onClick={approveProjectExport} disabled={exportBusy || (exportPreview.requiresAcknowledgement && !exportAcknowledged)}>{exportBusy ? 'Saving version…' : 'Approve and save new version'}</button></div></section></div>}
      {restorePreviewData && <div className="modal-backdrop open" onMouseDown={(event) => { if (event.target === event.currentTarget && !restoreBusy) setRestorePreviewData(null); }}><section className="modal export-preview-modal" role="dialog" aria-modal="true" aria-labelledby="restore-preview-title"><h2 id="restore-preview-title">Review restored copy</h2><p>{restorePreviewData.outputName} · {restorePreviewData.report.restoredCount} exact occurrences restored. The returned file and project versions remain unchanged until you save.</p><div className="export-preview-content"><h3>Restored output preview · {restorePreviewData.format}</h3><pre>{previewLines(restorePreviewData.preview).join('\n') || 'No supported editable text was found.'}</pre><h3>Adapter coverage</h3>{restorePreviewData.preview.warnings?.length ? <ul className="export-warning-list">{restorePreviewData.preview.warnings.map((warning, index) => <li key={`restore-warning-${index}`}>{warning}</li>)}</ul> : <p>No adapter coverage warnings were reported. This is not proof that all document content was examined.</p>}<h3>Unresolved tokens · {restorePreviewData.report.unresolvedCount}</h3>{restorePreviewData.report.unresolvedTokens.length ? <ul className="export-warning-list">{restorePreviewData.report.unresolvedTokens.map((item) => <li key={`${item.status}-${item.token}`}><code>{item.token}</code> · {item.count} · {item.status === 'altered' ? 'altered, left unchanged' : 'unknown or foreign, left unchanged'}</li>)}{restorePreviewData.report.unresolvedTokensTruncated && <li>Only the first 500 unique token values are listed; unresolved totals include all occurrences.</li>}</ul> : <p>No unresolved placeholder-like strings were detected in supported text.</p>}<p>Only exact intact tokens from this document and selected project version are restored. This report does not certify unsupported package content.</p></div><div className="modal-actions"><button className="text-btn" type="button" disabled={restoreBusy} onClick={() => setRestorePreviewData(null)}>Cancel</button><button className="primary-btn" type="button" disabled={restoreBusy} onClick={commitProjectRestoration}>{restoreBusy ? 'Saving…' : 'Save restored version'}</button></div></section></div>}
      {backupDialog && <div className="modal-backdrop open" onMouseDown={(event) => { if (event.target === event.currentTarget && !backupBusy) setBackupDialog(null); }}><form className="modal backup-modal" role="dialog" aria-modal="true" aria-labelledby="backup-dialog-title" onSubmit={backupDialog === 'create' ? createPortableBackup : restorePortableBackup}><h2 id="backup-dialog-title">Encrypted portable backup</h2><p>A passphrase-encrypted copy includes project documents, versions, and the private mapping. Keep the passphrase separately; it cannot be recovered.</p><div className="view-switch" role="tablist" aria-label="Backup action"><button type="button" className={backupDialog === 'create' ? 'active' : ''} role="tab" aria-selected={backupDialog === 'create'} onClick={() => setBackupDialog('create')} disabled={!currentProject}>Create</button><button type="button" className={backupDialog === 'restore' ? 'active' : ''} role="tab" aria-selected={backupDialog === 'restore'} onClick={() => setBackupDialog('restore')}>Restore</button></div>{backupDialog === 'restore' && <label className="project-field">Destination project folder<div className="backup-folder-field"><input value={backupDirectory} onChange={(event) => setBackupDirectory(event.target.value)} required placeholder="Choose an empty folder" /><button className="text-btn" type="button" onClick={chooseBackupDirectory} disabled={backupBusy}>Browse</button></div></label>}<label className="project-field">Backup passphrase<input type="password" value={backupPassphrase} onChange={(event) => setBackupPassphrase(event.target.value)} required minLength={12} maxLength={1024} autoComplete="new-password" placeholder="At least 12 characters" /></label><p className="backup-note">{backupDialog === 'create' ? 'Downloads a .blotbackup file protected by a memory-hard passphrase key.' : 'Choose the .blotbackup file after selecting an empty destination folder.'}</p><div className="modal-actions"><button className="text-btn" type="button" disabled={backupBusy} onClick={() => setBackupDialog(null)}>Cancel</button><button className="primary-btn" type="submit" disabled={backupBusy || !localToken || (backupDialog === 'create' && !currentProject)}>{backupBusy ? 'Working…' : backupDialog === 'create' ? 'Create encrypted backup' : 'Choose backup & restore'}</button></div></form></div>}
      {termContextMenu?.fileName === activeName && view === 'preview' && <div className="term-context-menu" data-term-context-menu role="menu" aria-label="Highlighted word actions" style={{ left: termContextMenu.x, top: termContextMenu.y }}><button type="button" role="menuitem" onClick={() => applyTermDecision(termContextMenu.target, 'suggested')}>Reset</button><button type="button" role="menuitem" onClick={() => applyTermDecision(termContextMenu.target, 'included')}>Include</button><button type="button" role="menuitem" onClick={() => applyTermDecision(termContextMenu.target, 'excluded')}>Exclude</button></div>}
      {toast && <div className="toast show" role="status" aria-live="polite">{toast}</div>}
      {modalOpen && <div className="modal-backdrop open" onMouseDown={(event) => { if (event.target === event.currentTarget) setModalOpen(false); }}><div className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><h2 id="modal-title">Import into Cedar briefing</h2><p>Files stay on this computer. This browser preview reads TXT, MD, and CSV. Office text extraction requires the local document engine.</p><label className="drop-zone"><strong>Choose a local file</strong><span>Up to 100 MB · no upload or cloud connection</span><input type="file" accept=".txt,.md,.csv,text/plain,text/csv" onChange={handleImport} aria-label="Choose a local text file" /></label><div className="modal-actions"><button className="text-btn" onClick={() => setModalOpen(false)}>Cancel</button></div></div></div>}
      {projectModalOpen && <div className="modal-backdrop open" onMouseDown={(event) => { if (event.target === event.currentTarget && !projectBusy) setProjectModalOpen(false); }}><form className="modal" role="dialog" aria-modal="true" aria-labelledby="project-modal-title" onSubmit={submitProject}><h2 id="project-modal-title">Local project folder</h2><p>Create a new workspace or open an existing Blot project. The folder and encrypted project state stay on this computer.</p><div className="view-switch" role="tablist" aria-label="Project action"><button type="button" className={projectAction === 'create' ? 'active' : ''} role="tab" aria-selected={projectAction === 'create'} onClick={() => setProjectAction('create')}>Create</button><button type="button" className={projectAction === 'open' ? 'active' : ''} role="tab" aria-selected={projectAction === 'open'} onClick={() => setProjectAction('open')}>Open existing</button></div>{projectAction === 'create' && <label className="project-field">Project name<input value={projectName} onChange={(event) => setProjectName(event.target.value)} required maxLength={100} autoFocus /></label>}<label className="project-field">Project folder<input value={projectDirectory} onChange={(event) => setProjectDirectory(event.target.value)} required placeholder="Choose a local folder" /></label><button className="text-btn" type="button" onClick={chooseProjectDirectory} disabled={!serviceAvailable || projectBusy}>{projectBusy ? 'Working…' : 'Browse on this computer'}</button>{!serviceAvailable && <p role="status">Run npm run dev:app to start the local services.</p>}<div className="modal-actions"><button className="text-btn" type="button" onClick={() => setProjectModalOpen(false)} disabled={projectBusy}>Cancel</button><button className="primary-btn" type="submit" disabled={!serviceAvailable || projectBusy}>{projectBusy ? 'Working…' : projectAction === 'create' ? 'Create project' : 'Open project'}</button></div></form></div>}
    </div>
  );
}
