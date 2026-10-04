import { useEffect, useState } from 'react';

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
  const [toast, setToast] = useState('');
  const [undo, setUndo] = useState(null);
  const [, setReplacementMaps] = useState({});

  const activeFile = files.find((file) => file.name === activeName) || files[0];
  const activeText = activeFile.rawText ?? activeFile.content.join(activeFile.lineEnding || '\n');
  const decisions = decisionSets[activeName] || { alex: 'suggested', cedar: 'suggested' };
  const confirmed = confirmationSets[activeName] || { alex: true, cedar: false };
  const getActiveMembers = (group) => confirmed[group.id] ? group.members : group.members.slice(0, 1);
  const visibleGroups = groups.filter((group) => (level >= group.level || decisions[group.id] !== 'suggested') && getActiveMembers(group).some((member) => countMatches(activeText, member)));
  const changeRows = visibleGroups.flatMap((group) => {
    const members = getActiveMembers(group);
    const occurrences = countGroupMatches(activeText, members);
    return occurrences ? [{ group, members, occurrences, decision: decisions[group.id] }] : [];
  });
  const matchCount = changeRows.reduce((sum, row) => sum + (row.decision === 'excluded' ? 0 : row.occurrences), 0);

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
    const terms = groups.flatMap((group) => getActiveMembers(group).map((member) => ({ member, group })))
      .sort((a, b) => b.member.length - a.member.length);
    if (!terms.length) return paragraph;
    const regex = new RegExp(`(${terms.map(({ member }) => member.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');
    return paragraph.split(regex).map((part, index) => {
      const found = terms.find(({ member }) => member.toLowerCase() === part.toLowerCase());
      if (!found) return part;
      const { group } = found;
      const decision = decisions[group.id];
      const isCandidate = level >= group.level;
      const className = decision === 'excluded' ? 'excluded' : decision === 'included' ? 'included' : isCandidate ? 'auto' : '';
      if (!className) return part;
      return <button key={`${group.id}-${index}`} type="button" className={`term ${className}`} onClick={() => changeDecision(group.id, decision === 'excluded' ? 'included' : 'excluded')} aria-label={`${part}: toggle obfuscation decision`}>{part}</button>;
    });
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
      setCurrentProject(data);
      setProjectModalOpen(false);
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

  const undoDecision = () => {
    if (!undo) return;
    setDecisionSets((current) => ({ ...current, [activeName]: { ...(current[activeName] || { alex: 'suggested', cedar: 'suggested' }), [undo.groupId]: undo.decision } }));
    setConfirmationSets((current) => ({ ...current, [activeName]: { ...(current[activeName] || { alex: true, cedar: false }), [undo.groupId]: undo.confirmed } }));
    setToast('Last group decision undone');
    setUndo(null);
  };

  const switchFile = (file) => {
    setActiveName(file.name);
    setSelectedGroup('alex');
    setView('preview');
    setUndo(null);
    setToast(`Opened ${file.name} · original remains unchanged`);
  };

  return (
    <div className="app-shell">
      <aside className="sidebar" data-od-id="sidebar">
        <div className="brand-row"><div className="brand-mark" aria-hidden="true">B</div><div><div className="brand-name">Blot</div><div className="brand-sub">private document workspace</div></div></div>
        <div className="side-section"><div className="side-label">Workspace</div><button className="side-link active" onClick={() => setToast('Review queue opened')}><span className="side-icon">◈</span> Review queue</button><button className="side-link" onClick={() => setToast('Showing all project files')}><span className="side-icon">□</span> All files <span style={{ marginLeft: 'auto', fontSize: 11 }}>{files.length}</span></button><button className="side-link" onClick={() => setToast('Activity is up to date')}><span className="side-icon">↺</span> Activity</button></div>
        <div className="side-section"><div className="side-label">Projects</div><button className={`side-link ${!currentProject ? 'active' : ''}`} onClick={() => setToast('Cedar briefing is the unsaved demo workspace')}><span className="side-icon">▣</span> Cedar briefing</button><button className="side-link" onClick={() => setToast('Launch notes is a demo project')}><span className="side-icon">▣</span> Launch notes</button>{currentProject && <button className="side-link active" title={currentProject.name}><span className="side-icon">▣</span>{currentProject.name}</button>}<button className="side-link" onClick={showProjectDialog}><span className="side-icon">＋</span> Add or open project</button></div>
        <div className="side-spacer" /><div className="local-badge"><span className="status-dot" style={{ background: serviceAvailable ? 'var(--success)' : 'var(--warn)' }} /><span><strong style={{ color: 'var(--accent-on)' }}>{serviceAvailable ? 'Local service ready' : 'UI preview mode'}</strong><br />{serviceAvailable ? 'Document content stays on this computer.' : 'Start FastAPI for local projects.'}</span></div>
      </aside>
      <main className="main">
        <header className="topbar"><div className="crumbs"><span>{currentProject?.name || 'Cedar briefing · demo'}</span><span>/</span><strong>{activeFile.name}</strong></div><div className="top-actions"><button className="text-btn" onClick={() => setToast('Encrypted backup is planned after project storage is complete')}>Encrypted backup</button><button className="icon-btn" type="button" aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} mode`} aria-pressed={theme === 'dark'} onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? '☀' : '☾'}</button><button className="icon-btn" aria-label="Open local settings" onClick={() => setToast('Local settings are available in the service configuration')}>•••</button><button className="primary-btn" onClick={() => currentProject ? setToast('Persistent file import is planned for the next stage') : setModalOpen(true)}>Import file</button></div></header>
        <div className="workspace">
          <div className="page-head"><div><p className="eyebrow">Document review · version 03</p><h1>Prepare a safe copy</h1><p className="subhead">Review suggested terms before this editable-text document leaves your computer. Similarity is a prompt, never a decision.</p></div><button className="primary-btn" onClick={exportCopy}>Export obfuscated copy</button></div>
          <section className="layout">
            <aside className="panel file-panel"><div className="panel-head"><span className="panel-title">{currentProject ? 'Demo preview files' : 'Project files'}</span><span className="panel-meta">{currentProject ? 'not saved' : `${files.length} items`}</span></div><div className="file-list">{files.map((file) => <button key={file.name} className={`file-item ${file.name === activeName ? 'active' : ''}`} onClick={() => switchFile(file)}><span className="file-type">{file.type}</span><span><span className="file-name">{file.name}</span><span className="file-status">{currentProject ? 'Synthetic sample · not saved' : file.status}</span></span><span className="file-check">{file.name === activeName ? '●' : file.status.includes('Ready') ? '✓' : file.status.includes('Restore') ? '↗' : ''}</span></button>)}</div></aside>
            <section className="panel review-panel">
              <div className="review-toolbar"><div className="review-title"><strong>{activeFile.name}</strong><span>Editable text view · {activeFile.content.length} {activeFile.type === 'PPTX' ? 'slides' : 'pages'} · local preview</span></div><div className="view-switch" role="tablist" aria-label="Document view"><button className={view === 'preview' ? 'active' : ''} role="tab" aria-selected={view === 'preview'} onClick={() => setView('preview')}>Preview</button><button className={view === 'changes' ? 'active' : ''} role="tab" aria-selected={view === 'changes'} onClick={() => setView('changes')}>Changes <span>{matchCount}</span></button></div></div>
              <div className="slider-area"><div className="slider-labels"><label htmlFor="sensitivity">Candidate breadth</label><span className="slider-value">Level {level} / 10</span></div><input id="sensitivity" type="range" min="1" max="10" value={level} onChange={(event) => setLevel(Number(event.target.value))} /><div className="range-notes"><span>Narrow · high confidence</span><span>All detected candidates</span></div></div>
              {view === 'preview' ? <div className="preview"><div className="preview-note"><span className="status-dot" /><span>{visibleGroups.length} suggested groups are visible at this level. Click a highlighted term to decide.</span></div><article className="doc-page"><div className="doc-kicker">BOARD UPDATE · 04 OCTOBER 2026</div><h2>{activeFile.heading}</h2>{activeFile.content.map((paragraph, index) => <p key={`${activeFile.name}-${index}`}>{renderParagraph(paragraph)}</p>)}<div className="legend"><span className="legend-item"><span className="legend-swatch" />Suggested</span><span className="legend-item"><span className="legend-swatch manual" />Manual decision</span><span className="legend-item">Click a term to inspect its group</span></div></article><div className="preview-foot"><span><strong>{matchCount}</strong> occurrences will change at level <strong>{level}</strong></span><span>Original stays unchanged · new version on export</span></div>{undo?.file === activeName && <button className="small-btn undo-button" onClick={undoDecision}>Undo last decision</button>}</div> : <div className="preview changes-pane"><div className="preview-note"><span className="status-dot" /><span>Export diff for version 03</span></div><table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}><thead><tr style={{ color: 'var(--muted)', font: '11px var(--font-mono)', textAlign: 'left' }}><th style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>OCCURRENCE</th><th style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>REPLACEMENT</th><th style={{ padding: 8, borderBottom: '1px solid var(--border)' }}>DECISION</th></tr></thead><tbody>{changeRows.map(({ group, occurrences, decision }) => <tr key={group.id}><td style={{ padding: '12px 8px', borderBottom: '1px solid var(--border-soft)' }}>{group.term} · {occurrences} {occurrences === 1 ? 'match' : 'matches'}</td><td style={{ padding: '12px 8px', borderBottom: '1px solid var(--border-soft)', fontFamily: 'var(--font-mono)', color: 'var(--accent)' }}>[[{group.token}]]</td><td style={{ padding: '12px 8px', borderBottom: '1px solid var(--border-soft)' }}>{decision === 'excluded' ? 'Excluded' : decision === 'included' ? 'Included' : 'Suggested'}</td></tr>)}</tbody></table></div>}
            </section>
            <aside className="right-stack">
              <section className="panel"><div className="panel-head"><span className="panel-title">Suggested groups</span><span className="panel-meta">{visibleGroups.length} groups</span></div><div className="graph-list">{visibleGroups.length ? visibleGroups.map((group) => <div key={group.id} className={`graph-card ${selectedGroup === group.id ? 'selected' : ''}`} onClick={() => setSelectedGroup(group.id)}><div className="graph-card-head"><span className="graph-term">{group.term}</span><span className="confidence">{group.confidence} match</span></div><p className="graph-reason">{group.reason}</p><div className="member-row">{group.members.map((member, index) => <span key={member} className={`member ${confirmed[group.id] || (index === 0 && group.id === 'alex') ? 'confirmed' : ''}`}>{member} · {countMatches(activeText, member)}</span>)}</div><div className="graph-actions"><button className="small-btn primary" onClick={(event) => { event.stopPropagation(); changeDecision(group.id, 'included'); }}>{decisions[group.id] === 'included' ? 'Included' : 'Include group'}</button><button className="small-btn" onClick={(event) => { event.stopPropagation(); changeDecision(group.id, 'excluded'); }}>Exclude</button></div></div>) : <p style={{ padding: 10, color: 'var(--muted)', fontSize: 12 }}>No detected groups at this level.</p>}</div></section>
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
