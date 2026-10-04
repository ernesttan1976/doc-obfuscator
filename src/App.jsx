import { useEffect, useState } from 'react';
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
    reason: 'Alias + formatting variant',
    token: 'T_001',
    level: 1,
    confirmed: true,
  },
  {
    id: 'cedar',
    term: 'Project Cedar',
    members: ['Project Cedar', 'Cedar'],
    confidence: '89%',
    reason: 'Contextual similarity · confirm before grouping',
    token: 'T_002',
    level: 3,
    confirmed: false,
  },
];

const initialFiles = [
  { name: 'board-update.docx', type: 'DOCX', status: 'Needs review · v03', content: sampleText, heading: 'Project Cedar: Q4 operating brief' },
  { name: 'launch-brief.pptx', type: 'PPTX', status: 'Ready to send', content: ['Alex Tan will share the Project Cedar launch window with the partner team.', 'Cedar milestones remain internal until the review is complete.'], heading: 'Launch briefing' },
  { name: 'notes.md', type: 'MD', status: 'Original', content: ['Project Cedar notes', 'Owner: Alex Tan (A. Tan). Keep the Cedar reference private until sign-off.'], heading: 'Working notes' },
  { name: 'agent-return.pptx', type: 'PPTX', status: 'Restore available', content: ['Project owner: [[T_001]]', 'Project: [[T_002]]'], heading: 'Returned presentation' },
];

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
  const [theme, setTheme] = useState(() => localStorage.getItem('blot-theme') === 'dark' ? 'dark' : 'light');
  const [modalOpen, setModalOpen] = useState(false);
  const [projectModalOpen, setProjectModalOpen] = useState(false);
  const [projectAction, setProjectAction] = useState('create');
  const [projectName, setProjectName] = useState('');
  const [projectDirectory, setProjectDirectory] = useState('');
  const [localToken, setLocalToken] = useState('');
  const [serviceAvailable, setServiceAvailable] = useState(false);
  const [currentProject, setCurrentProject] = useState(null);
  const [projectBusy, setProjectBusy] = useState(false);
  const [manualPhrase, setManualPhrase] = useState('');
  const [mergeGroupIds, setMergeGroupIds] = useState([]);
  const [toast, setToast] = useState('');
  const [undo, setUndo] = useState(null);
  const [, setReplacementMaps] = useState({});

  const activeFile = files.find((file) => (file.id || file.name) === activeName) || files[0];
  const activeText = activeFile.rawText ?? activeFile.content.join(activeFile.lineEnding || '\n');
  const previewSections = activeFile.previewSections || [];
  const activeSection = previewSections[sectionIndex];
  const decisions = decisionSets[activeName] || { alex: 'suggested', cedar: 'suggested' };
  const confirmed = confirmationSets[activeName] || { alex: true, cedar: false };
  const getActiveMembers = (group) => confirmed[group.id] ? group.members : group.members.slice(0, 1);
  const visibleGroups = activeFile.isProjectDocument ? [] : groups.filter((group) => (level >= group.level || decisions[group.id] !== 'suggested') && getActiveMembers(group).some((member) => countMatches(activeText, member)));
  const changeRows = visibleGroups.flatMap((group) => {
    const members = getActiveMembers(group);
    const occurrences = countGroupMatches(activeText, members);
    return occurrences ? [{ group, members, occurrences, decision: decisions[group.id] }] : [];
  });
  const matchCount = changeRows.reduce((sum, row) => sum + (row.decision === 'excluded' ? 0 : row.occurrences), 0);
  const candidates = activeFile.candidates || [];
  const visibleCandidates = candidates.filter((candidate) => (
    candidate.level <= level || candidate.decision !== 'suggested'
  ));
  const candidatesById = Object.fromEntries(candidates.map((candidate) => [candidate.id, candidate]));
  const candidateGroups = activeFile.candidateGroups || [];
  const proposals = (activeFile.proposals || []).filter((proposal) => (
    candidatesById[proposal.sourceId]
    && candidatesById[proposal.targetId]
    && !candidateGroups.some((group) => (
      group.candidateIds.includes(proposal.sourceId) && group.candidateIds.includes(proposal.targetId)
    ))
  ));
  const candidateDecisionCounts = candidates.reduce((counts, candidate) => {
    counts[candidate.decision] = (counts[candidate.decision] || 0) + 1;
    return counts;
  }, { suggested: 0, included: 0, excluded: 0 });
  const belowLevelCount = candidates.filter((candidate) => candidate.level > level && candidate.decision === 'suggested').length;
  const previewCoverage = activeFile.previewCoverage;
  const previewWarnings = activeFile.previewWarnings || [];
  const unsupportedPartCount = previewCoverage?.unsupportedPartCount || 0;

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
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('blot-theme', theme);
  }, [theme]);
  useEffect(() => {
    localStorage.setItem('blot-level', String(level));
  }, [level]);
  useEffect(() => {
    const captureSelectedPhrase = () => {
      if (!activeFile.isProjectDocument) return;
      const selection = window.getSelection();
      const selected = selection?.toString().trim() || '';
      const page = document.querySelector('.doc-page');
      if (selected && selected.length <= 256 && page?.contains(selection.anchorNode)) {
        setManualPhrase(selected);
      }
    };
    document.addEventListener('mouseup', captureSelectedPhrase);
    return () => document.removeEventListener('mouseup', captureSelectedPhrase);
  }, [activeFile.isProjectDocument]);
  useEffect(() => {
    if (!toast) return undefined;
    const timer = window.setTimeout(() => setToast(''), 2600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const changeDecision = (groupId, decision) => {
    setUndo({ file: activeName, groupId, decision: decisions[groupId], confirmed: confirmed[groupId] });
    setDecisionSets((current) => ({ ...current, [activeName]: { ...(current[activeName] || { alex: 'suggested', cedar: 'suggested' }), [groupId]: decision } }));
    if (decision === 'included') setConfirmationSets((current) => ({ ...current, [activeName]: { ...(current[activeName] || { alex: true, cedar: false }), [groupId]: true } }));
    const group = groups.find((item) => item.id === groupId);
    const label = decision === 'included' ? 'Included' : decision === 'excluded' ? 'Excluded' : 'Reset';
    setToast(`${label} ${group.term} group across this document`);
  };

  const renderParagraph = (paragraph) => {
    const terms = (activeFile.isProjectDocument
      ? visibleCandidates.map((candidate) => ({ member: candidate.term, candidate }))
      : groups.flatMap((group) => getActiveMembers(group).map((member) => ({ member, group })))
    ).sort((a, b) => b.member.length - a.member.length);
    if (!terms.length) return paragraph;
    const regex = new RegExp(`(${terms.map(({ member }) => member.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
    return paragraph.split(regex).map((part, index) => {
      const found = terms.find(({ member }) => member.toLowerCase() === part.toLowerCase());
      if (!found) return part;
      if (found.candidate) {
        const { candidate } = found;
        const className = candidate.decision === 'excluded'
          ? 'excluded'
          : candidate.decision === 'included'
            ? 'included'
            : 'auto';
        return <button key={`${candidate.id}-${index}`} type="button" className={`term ${className}`} onClick={() => setProjectCandidateDecision(candidate, candidate.decision === 'included' ? 'excluded' : 'included')} aria-label={`${part}: candidate ${candidate.decision}; activate to toggle Include/Exclude`}>{part}</button>;
      }
      const { group } = found;
      const decision = decisions[group.id];
      const isCandidate = level >= group.level;
      const className = decision === 'excluded' ? 'excluded' : decision === 'included' ? 'included' : isCandidate ? 'auto' : '';
      if (!className) return part;
      return <button key={`${group.id}-${index}`} type="button" className={`term ${className}`} onClick={() => changeDecision(group.id, decision === 'excluded' ? 'included' : 'excluded')} aria-label={`${part}: toggle obfuscation decision`}>{part}</button>;
    });
  };

  const loadProjectDocumentCandidates = async (documentId, manualTerms = []) => {
    if (!projectDirectory || !localToken) return false;
    setFiles((current) => current.map((file) => (
      file.id === documentId ? { ...file, candidateLoading: true, candidateError: '' } : file
    )));
    try {
      const response = await fetch('/api/projects/document-candidates', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({ directory: projectDirectory, document_id: documentId, manual_terms: manualTerms }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not analyze supported text');
      setFiles((current) => current.map((file) => (
        file.id === documentId
          ? {
            ...file,
            candidates: data.candidates,
            proposals: data.proposals,
            candidateGroups: data.groups,
            candidateLimitReached: data.candidateLimitReached || data.proposalLimitReached,
            candidateLoaded: true,
            candidateLoading: false,
            candidateError: '',
          }
          : file
      )));
      return true;
    } catch (error) {
      setFiles((current) => current.map((file) => (
        file.id === documentId ? { ...file, candidateLoading: false, candidateLoaded: false, candidateError: error.message } : file
      )));
      return false;
    }
  };

  const loadProjectDocumentPreview = async (documentId) => {
    if (!projectDirectory || !localToken) return;
    setFiles((current) => current.map((file) => (
      file.id === documentId ? { ...file, previewLoading: true } : file
    )));
    try {
      const response = await fetch('/api/projects/document-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Local-App-Token': localToken },
        body: JSON.stringify({ directory: projectDirectory, document_id: documentId }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Could not parse this document for preview');
      const content = previewLines(data);
      const previewSections = (data.previewSections || []).map((section) => {
        const slideMatch = data.format === 'PPTX' && section.part.match(/(?:^|\/)slide(\d+)\.xml$/i);
        return {
          label: slideMatch ? `Slide ${slideMatch[1]}` : section.part,
          content: section.text.split(/\r\n|\r|\n/),
        };
      });
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
            previewLoaded: true,
            previewLoading: false,
            status: 'Parsed preview · original unchanged',
          }
          : file
      )));
      await loadProjectDocumentCandidates(documentId);
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
      setFiles((current) => current.map((file) => (
        file.id === activeFile.id
          ? { ...file, candidates: file.candidates.map((item) => item.id === updated.id ? updated : item) }
          : file
      )));
      setUndo({ file: activeName, candidateId: candidate.id, decision: candidate.decision, isCandidate: true });
      setToast(`${decision === 'included' ? 'Included' : decision === 'excluded' ? 'Excluded' : 'Reset'} ${candidate.term}`);
    } catch (error) {
      setToast(error.message || 'Could not save this candidate decision');
    }
  };

  const addManualCandidate = async (event) => {
    event.preventDefault();
    const phrase = manualPhrase.trim();
    if (!phrase) return;
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
      setFiles((current) => current.map((file) => (
        file.id === activeFile.id ? { ...file, candidateGroups: data.groups } : file
      )));
      setMergeGroupIds([]);
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
        name: document.name,
        type: document.type,
        status: 'Saved original · v01',
        content: ['Loading a local parsed preview…'],
        rawText: '',
        heading: `${document.name} · saved original`,
        isProjectDocument: true,
      }));
      setFiles((current) => [...imported, ...current.filter((file) => !imported.some((document) => document.id === file.id))]);
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

  const exportCopy = () => {
    if (currentProject) {
      setToast('Export is not yet connected to saved project documents. No project files were changed.');
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
      setToast('No terms are selected at this level. Include a group or adjust the slider first.');
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

  const restorePreview = () => setToast(currentProject
    ? 'Restoration is not yet connected to saved project documents.'
    : 'Restoration requires the local DOCX/PPTX document engine; no file was changed.');

  const chooseProjectDirectory = async () => {
    if (!localToken) {
      setToast('Start the local service before choosing a project folder');
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
        name: document.name,
        type: document.type,
        status: 'Saved original · v01',
        content: ['Loading a local parsed preview…'],
        rawText: '',
        heading: `${document.name} · saved original`,
        isProjectDocument: true,
      }));
      setCurrentProject(data);
      setFiles([...savedDocuments, ...initialFiles]);
      setActiveName(savedDocuments[0]?.id || initialFiles[0].name);
      setSectionIndex(0);
      setDenseText(false);
      setProjectModalOpen(false);
      if (savedDocuments[0]) await loadProjectDocumentPreview(savedDocuments[0].id);
      setToast(`${projectAction === 'create' ? 'Created' : 'Opened'} local project “${data.name}”`);
    } catch (error) {
      setToast(error.message || 'Could not open this project');
    } finally {
      setProjectBusy(false);
    }
  };

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
        setFiles((current) => current.map((file) => (
          file.id === activeFile.id
            ? { ...file, candidates: file.candidates.map((item) => item.id === updated.id ? updated : item) }
            : file
        )));
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
    setActiveName(file.id || file.name);
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
    setManualPhrase('');
    setToast(`Opened ${file.name} · original remains unchanged`);
    if (file.isProjectDocument && !file.previewLoaded && !file.previewLoading) {
      void loadProjectDocumentPreview(file.id);
    }
    if (file.isProjectDocument && file.previewLoaded && !file.candidateLoaded && !file.candidateLoading) {
      void loadProjectDocumentCandidates(file.id);
    }
  };

  return (
    <div className="app-shell" data-project-preview={activeFile.isProjectDocument ? 'true' : undefined} data-preview-error={activeFile.previewError ? 'true' : undefined} data-dense-text={denseText ? 'true' : undefined}>
      <aside className="sidebar" data-od-id="sidebar">
        <div className="brand-row"><div className="brand-mark" aria-hidden="true">B</div><div><div className="brand-name">Blot</div><div className="brand-sub">private document workspace</div></div></div>
        <div className="side-section"><div className="side-label">Workspace</div><button className="side-link active" onClick={() => setToast('Review queue opened')}><span className="side-icon">◈</span> Review queue</button><button className="side-link" onClick={() => setToast('Showing all project files')}><span className="side-icon">□</span> All files <span style={{ marginLeft: 'auto', fontSize: 11 }}>{files.length}</span></button><button className="side-link" onClick={() => setToast('Activity is up to date')}><span className="side-icon">↺</span> Activity</button></div>
        <div className="side-section"><div className="side-label">Projects</div><button className={`side-link ${!currentProject ? 'active' : ''}`} onClick={() => setToast('Cedar briefing is the unsaved demo workspace')}><span className="side-icon">▣</span> Cedar briefing</button><button className="side-link" onClick={() => setToast('Launch notes is a demo project')}><span className="side-icon">▣</span> Launch notes</button>{currentProject && <button className="side-link active" title={currentProject.name}><span className="side-icon">▣</span>{currentProject.name}</button>}<button className="side-link" onClick={showProjectDialog}><span className="side-icon">＋</span> Add or open project</button></div>
        <div className="side-spacer" /><div className="local-badge"><span className="status-dot" style={{ background: serviceAvailable ? 'var(--success)' : 'var(--warn)' }} /><span><strong style={{ color: 'var(--accent-on)' }}>{serviceAvailable ? 'Local service ready' : 'UI preview mode'}</strong><br />{serviceAvailable ? 'Document content stays on this computer.' : 'Start FastAPI for local projects.'}</span></div>
      </aside>
      <main className="main">
        <header className="topbar"><div className="crumbs"><span>{currentProject?.name || 'Cedar briefing · demo'}</span><span>/</span><strong>{activeFile.name}</strong></div><div className="top-actions"><button className="text-btn" onClick={() => setToast('Encrypted backup is planned for release hardening')}>Encrypted backup</button><button className="icon-btn" type="button" aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`} aria-pressed={theme === 'dark'} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? '☀' : '☾'}</button><button className="icon-btn" aria-label="Open local settings" onClick={() => setToast('Local settings are available in the service configuration')}>•••</button><button className="primary-btn" disabled={currentProject && projectBusy} onClick={() => currentProject ? importProjectDocuments() : setModalOpen(true)}>{projectBusy ? 'Working…' : 'Import file'}</button></div></header>
        <div className="workspace">
          <div className="page-head"><div><p className="eyebrow">Document review · version 03</p><h1>Prepare a safe copy</h1><p className="subhead">Review suggested terms before this editable-text document leaves your computer. Similarity is a prompt, never a decision.</p></div><button className="primary-btn" onClick={exportCopy}>Export obfuscated copy</button></div>
          <section className="layout">
            <aside className="panel file-panel"><div className="panel-head"><span className="panel-title">{currentProject ? 'Project documents' : 'Project files'}</span><span className="panel-meta">{currentProject ? `${files.filter((file) => file.isProjectDocument).length} saved` : `${files.length} items`}</span></div><div className="file-list">{files.map((file) => <button key={file.id || file.name} className={`file-item ${(file.id || file.name) === activeName ? 'active' : ''}`} onClick={() => switchFile(file)}><span className="file-type">{file.type}</span><span><span className="file-name">{file.name}</span><span className="file-status">{file.isProjectDocument ? file.status : currentProject ? 'Synthetic sample · not saved' : file.status}</span></span><span className="file-check">{(file.id || file.name) === activeName ? '●' : file.status.includes('Ready') ? '✓' : file.status.includes('Restore') ? '↗' : ''}</span></button>)}</div></aside>
            <section className="panel review-panel">
              <div className="review-toolbar"><div className="review-title"><strong>{activeFile.name}</strong><span>{activeFile.isProjectDocument ? 'Original source · not parsed or modified' : `Editable text view · ${activeFile.content.length} ${activeFile.type === 'PPTX' ? 'slides' : 'pages'} · local preview`}</span></div><div className="view-switch" role="tablist" aria-label="Document view"><button className={view === 'preview' ? 'active' : ''} role="tab" aria-selected={view === 'preview'} onClick={() => setView('preview')}>Preview</button><button className={view === 'changes' ? 'active' : ''} role="tab" aria-selected={view === 'changes'} onClick={() => setView('changes')}>Changes <span>{matchCount}</span></button></div></div>
              <div className="slider-area"><div className="slider-labels"><label htmlFor="sensitivity">Candidate breadth</label><span className="slider-value">Level {level} / 10</span></div><input id="sensitivity" type="range" min="1" max="10" value={level} aria-valuetext={`Level ${level} of 10 candidate breadth`} onChange={(event) => setLevel(Number(event.target.value))} /><div className="range-notes"><span>Narrow · fewer candidate types</span><span>All detected candidates</span></div></div>
              {view === 'preview' ? <div className="preview"><div className="preview-note"><span className="status-dot" /><span>{activeFile.isProjectDocument ? (activeFile.candidateLoading ? 'Scanning supported editable text locally…' : `${visibleCandidates.length} candidates shown at level ${level}. Review decisions below; only supported editable text is scanned.`) : `${visibleGroups.length} suggested groups are visible at this level. Click a highlighted term to decide.`}</span></div><article className="doc-page"><div className="doc-kicker">BOARD UPDATE · 04 OCTOBER 2026</div><h2>{activeFile.heading}</h2>{activeFile.content.map((paragraph, index) => <p key={`${activeFile.name}-${index}`}>{renderParagraph(paragraph)}</p>)}<div className="legend"><span className="legend-item"><span className="legend-swatch" />Suggested</span><span className="legend-item"><span className="legend-swatch manual" />Manual decision</span><span className="legend-item">Click a term to inspect its group</span></div></article><div className="preview-foot"><span><strong>{activeFile.isProjectDocument ? visibleCandidates.reduce((sum, candidate) => sum + (candidate.decision === 'excluded' ? 0 : candidate.occurrenceCount), 0) : matchCount}</strong> included or suggested occurrences at level <strong>{level}</strong></span><span>Original stays unchanged</span></div>{undo?.file === activeName && <button className="small-btn undo-button" onClick={undoDecision}>Undo last decision</button>}</div> : <div className="preview changes-pane"><div className="preview-note"><span className="status-dot" /><span>Export diff for version 03</span></div><table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}><thead><tr style={{ color: 'var(--muted)', font: '11px var(--font-mono)', textAlign: 'left' }}><th style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>OCCURRENCE</th><th style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>REPLACEMENT</th><th style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>DECISION</th></tr></thead><tbody>{changeRows.map(({ group, occurrences, decision }) => <tr key={group.id}><td style={{ padding: '12px 8px', borderBottom: '1px solid var(--border-soft)' }}>{group.term} · {occurrences} {occurrences === 1 ? 'match' : 'matches'}</td><td style={{ padding: '12px 8px', borderBottom: '1px solid var(--border-soft)', fontFamily: 'var(--font-mono)', color: 'var(--accent)' }}>[[{group.token}]]</td><td style={{ padding: '12px 8px', borderBottom: '1px solid var(--border-soft)' }}>{decision === 'excluded' ? 'Excluded' : decision === 'included' ? 'Included' : 'Suggested'}</td></tr>)}</tbody></table></div>}
            </section>
            <aside className="right-stack">
              {activeFile.isProjectDocument ? <section className="panel candidate-panel">
                <div className="panel-head"><span className="panel-title">Candidate review</span><span className="panel-meta">{visibleCandidates.length} shown</span></div>
                <label className="dense-toggle"><input type="checkbox" checked={denseText} onChange={(event) => setDenseText(event.target.checked)} /> Dense text view</label>
                {previewSections.length > 1 && <div className="preview-navigation" role="group" aria-label="Preview sections">
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
                  {belowLevelCount > 0 && <span><strong>{belowLevelCount}</strong> suggested below this level</span>}
                  <span><strong>{proposals.length}</strong> proposals not in a group</span>
                </div>
                <div className="graph-list">
                  {activeFile.candidateLoading && <p role="status">Scanning supported text locally…</p>}
                  {activeFile.candidateError && <p className="candidate-error" role="alert">{activeFile.candidateError} <button className="small-btn" onClick={() => loadProjectDocumentCandidates(activeFile.id)}>Retry</button></p>}
                  {activeFile.candidateLimitReached && <p className="candidate-limit">Candidate or proposal list reached its display limit.</p>}
                  {visibleCandidates.map((candidate) => <div className="graph-card candidate-card" key={candidate.id}>
                    <div className="graph-card-head"><span className="graph-term">{candidate.term}</span><span className="confidence">Level {candidate.level}</span></div>
                    <p className="graph-reason">{candidate.category.replaceAll('_', ' ').toLowerCase()} · {candidate.occurrenceCount} {candidate.occurrenceCount === 1 ? 'occurrence' : 'occurrences'} · {candidate.source}</p>
                    {candidate.occurrences[0] && <p className="candidate-location">{candidate.occurrences[0].location}{candidate.occurrencesTruncated ? ' · locations truncated' : ''}</p>}
                    <div className="graph-actions">
                      <button className={`small-btn ${candidate.decision === 'included' ? 'primary' : ''}`} onClick={() => setProjectCandidateDecision(candidate, candidate.decision === 'included' ? 'suggested' : 'included')}>{candidate.decision === 'included' ? 'Included' : 'Include'}</button>
                      <button className={`small-btn ${candidate.decision === 'excluded' ? 'selected' : ''}`} onClick={() => setProjectCandidateDecision(candidate, candidate.decision === 'excluded' ? 'suggested' : 'excluded')}>{candidate.decision === 'excluded' ? 'Excluded' : 'Exclude'}</button>
                    </div>
                  </div>)}
                  {!activeFile.candidateLoading && !activeFile.candidateError && visibleCandidates.length === 0 && <p style={{ padding: 10, color: 'var(--muted)', fontSize: 12 }}>{activeFile.previewError ? 'Candidate analysis requires a readable local preview.' : candidates.length ? 'No suggested candidates at this level. Included and excluded decisions remain visible.' : 'No candidates found in supported editable text.'}</p>}
                </div>
                {proposals.length > 0 && <div className="candidate-subsection"><div className="candidate-subhead">Similarity proposals · confirm before grouping</div>{proposals.slice(0, 50).map((proposal) => <div className="proposal-row" key={proposal.id}><div><strong>{candidatesById[proposal.sourceId].term} ↔ {candidatesById[proposal.targetId].term}</strong><span>{proposal.reason}</span></div><button className="small-btn" onClick={() => applyCandidateGroupOperation('add', [proposal.sourceId, proposal.targetId])}>Confirm group</button></div>)}</div>}
                {candidateGroups.length > 0 && <div className="candidate-subsection"><div className="candidate-subhead">Confirmed groups</div>{candidateGroups.map((group) => <div className="confirmed-group" key={group.id}><label><input type="checkbox" checked={mergeGroupIds.includes(group.id)} onChange={(event) => setMergeGroupIds((current) => event.target.checked ? [...current, group.id] : current.filter((id) => id !== group.id))} /> Merge group</label>{group.candidateIds.map((candidateId) => <div className="confirmed-member" key={candidateId}><span>{candidatesById[candidateId]?.term || 'Candidate'}</span><div>{group.candidateIds.length > 1 && <button className="small-btn" onClick={() => applyCandidateGroupOperation('split', [candidateId], group.id)}>Split out</button>}<button className="small-btn" onClick={() => applyCandidateGroupOperation('remove', [candidateId], group.id)}>Remove</button></div></div>)}</div>)}<button className="small-btn" disabled={mergeGroupIds.length < 2} onClick={() => applyCandidateGroupOperation('merge', [], undefined, mergeGroupIds)}>Merge selected groups</button></div>}
              </section> : <section className="panel"><div className="panel-head"><span className="panel-title">Suggested groups</span><span className="panel-meta">{visibleGroups.length} groups</span></div><div className="graph-list">{visibleGroups.length ? visibleGroups.map((group) => <div key={group.id} className={`graph-card ${selectedGroup === group.id ? 'selected' : ''}`} onClick={() => setSelectedGroup(group.id)}><div className="graph-card-head"><span className="graph-term">{group.term}</span><span className="confidence">{group.confidence} match</span></div><p className="graph-reason">{group.reason}</p><div className="member-row">{group.members.map((member, index) => <span key={member} className={`member ${confirmed[group.id] || (index === 0 && group.id === 'alex') ? 'confirmed' : ''}`}>{member} · {countMatches(activeText, member)}</span>)}</div><div className="graph-actions"><button className="small-btn primary" onClick={(event) => { event.stopPropagation(); changeDecision(group.id, 'included'); }}>{decisions[group.id] === 'included' ? 'Included' : 'Include group'}</button><button className="small-btn" onClick={(event) => { event.stopPropagation(); changeDecision(group.id, 'excluded'); }}>Exclude</button></div></div>) : <p style={{ padding: 10, color: 'var(--muted)', fontSize: 12 }}>No detected groups at this level.</p>}</div></section>}
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
              <section className="panel status-card"><div className="panel-head" style={{ padding: '0 0 12px', border: 0, minHeight: 'auto' }}><span className="panel-title">Restore returned file</span><span className="panel-meta">1 ready</span></div><p style={{ margin: '0 0 12px', color: 'var(--muted)', fontSize: 12, lineHeight: 1.45 }}>agent-return.pptx has placeholders from this project's sample graph.</p><button className="primary-btn" style={{ width: '100%' }} onClick={restorePreview}>Preview restoration</button></section>
            </aside>
          </section>
        </div>
      </main>
      {toast && <div className="toast show" role="status" aria-live="polite">{toast}</div>}
      {modalOpen && <div className="modal-backdrop open" onMouseDown={(event) => { if (event.target === event.currentTarget) setModalOpen(false); }}><div className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><h2 id="modal-title">Import into Cedar briefing</h2><p>Files stay on this computer. This browser preview reads TXT, MD, and CSV. Office text extraction requires the local document engine.</p><label className="drop-zone"><strong>Choose a local file</strong><span>Up to 100 MB · no upload or cloud connection</span><input type="file" accept=".txt,.md,.csv,text/plain,text/csv" onChange={handleImport} aria-label="Choose a local text file" /></label><div className="modal-actions"><button className="text-btn" onClick={() => setModalOpen(false)}>Cancel</button></div></div></div>}
      {projectModalOpen && <div className="modal-backdrop open" onMouseDown={(event) => { if (event.target === event.currentTarget && !projectBusy) setProjectModalOpen(false); }}><form className="modal" role="dialog" aria-modal="true" aria-labelledby="project-modal-title" onSubmit={submitProject}><h2 id="project-modal-title">Local project folder</h2><p>Create a new workspace or open an existing Blot project. The folder and encrypted project state stay on this computer.</p><div className="view-switch" role="tablist" aria-label="Project action"><button type="button" className={projectAction === 'create' ? 'active' : ''} role="tab" aria-selected={projectAction === 'create'} onClick={() => setProjectAction('create')}>Create</button><button type="button" className={projectAction === 'open' ? 'active' : ''} role="tab" aria-selected={projectAction === 'open'} onClick={() => setProjectAction('open')}>Open existing</button></div>{projectAction === 'create' && <label className="project-field">Project name<input value={projectName} onChange={(event) => setProjectName(event.target.value)} required maxLength={100} autoFocus /></label>}<label className="project-field">Project folder<input value={projectDirectory} onChange={(event) => setProjectDirectory(event.target.value)} required placeholder="Choose a local folder" /></label><button className="text-btn" type="button" onClick={chooseProjectDirectory} disabled={!serviceAvailable || projectBusy}>{projectBusy ? 'Working…' : 'Browse on this computer'}</button>{!serviceAvailable && <p role="status">Start FastAPI to create or open a local project.</p>}<div className="modal-actions"><button className="text-btn" type="button" onClick={() => setProjectModalOpen(false)} disabled={projectBusy}>Cancel</button><button className="primary-btn" type="submit" disabled={!serviceAvailable || projectBusy}>{projectBusy ? 'Working…' : projectAction === 'create' ? 'Create project' : 'Open project'}</button></div></form></div>}
    </div>
  );
}
