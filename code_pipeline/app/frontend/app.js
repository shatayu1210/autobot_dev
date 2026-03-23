const createForm = document.getElementById('create-form');
const topicInput = document.getElementById('topic-input');
const createButton = document.getElementById('create-button');
const progressContainer = document.getElementById('progress-container');
const statusText = document.getElementById('status-text');
const landing = document.getElementById('landing');
const resultSection = document.getElementById('result-section');
const runAgainBtn = document.getElementById('run-again-btn');
const copyDiffBtn = document.getElementById('copy-diff-btn');

const sessionId = 'session-' + Math.random().toString(36).substring(2, 15);

// --- Step tracking ---

const STEPS = ['planner', 'patcher', 'critic', 'done'];

function setActiveStep(stepName) {
    STEPS.forEach(s => {
        const el = document.getElementById('step-' + s);
        if (el) el.classList.remove('active', 'completed');
    });

    let found = false;
    for (const s of STEPS) {
        const el = document.getElementById('step-' + s);
        if (!el) continue;
        if (s === stepName) {
            el.classList.add('active');
            found = true;
        } else if (!found) {
            el.classList.add('completed');
        }
    }
}

function showProgress() {
    progressContainer.classList.remove('hidden');
    topicInput.disabled = true;
    createButton.disabled = true;
    createButton.innerHTML = '<span>Running...</span>';
    setActiveStep('planner');
}

function updateStatus(text) {
    statusText.textContent = text;

    const lower = text.toLowerCase();
    if (lower.includes('planner') || lower.includes('analyzing') || lower.includes('plan')) {
        setActiveStep('planner');
    } else if (lower.includes('patcher') || lower.includes('diff') || lower.includes('patch')) {
        setActiveStep('patcher');
    } else if (lower.includes('critic') || lower.includes('evaluat') || lower.includes('verdict')) {
        setActiveStep('critic');
    } else if (lower.includes('finaliz') || lower.includes('done') || lower.includes('result')) {
        setActiveStep('done');
    }
}

// --- Result rendering ---

/**
 * Extracts named sections from the pipeline's markdown output.
 * The patch_output_agent produces sections delimited by ### headings.
 */
function parseResult(text) {
    const sections = {};

    // Determine overall status from the h2 heading
    const h2Match = text.match(/^##\s+(.+)$/m);
    sections.status = h2Match ? h2Match[1].trim() : '';

    // Extract Plan Summary
    const planMatch = text.match(/###\s+Plan Summary\s*\n([\s\S]*?)(?=###|$)/);
    sections.plan = planMatch ? planMatch[1].trim() : '';

    // Extract Unified Diff (inside ```diff ... ```)
    const diffMatch = text.match(/```diff\s*\n([\s\S]*?)```/);
    sections.diff = diffMatch ? diffMatch[1].trim() : '';

    // Extract Critic Feedback
    const feedbackMatch = text.match(/###\s+Critic Feedback\s*\n([\s\S]*?)(?=###|$)/);
    sections.feedback = feedbackMatch ? feedbackMatch[1].trim() : '';

    return sections;
}

function escapeHtml(str) {
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

/** Minimal markdown → HTML: bold, inline code, bullet lists, paragraphs. */
function renderMarkdown(text) {
    if (!text) return '';
    return text
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
        .replace(/(<li>.*<\/li>\n?)+/g, m => `<ul>${m}</ul>`)
        .replace(/^(\d+)\. (.+)$/gm, '<li>$2</li>')
        .replace(/\n\n/g, '</p><p>')
        .replace(/^(?!<[uo]l|<li)(.+)$/gm, (m) => m.startsWith('<') ? m : m)
        .split('\n\n').map(p => p.startsWith('<') ? p : `<p>${p}</p>`).join('');
}

/** Colorize diff lines for display */
function renderDiff(diffText) {
    return diffText.split('\n').map(line => {
        const safe = escapeHtml(line);
        if (line.startsWith('+++') || line.startsWith('---')) {
            return `<span class="diff-file">${safe}</span>`;
        } else if (line.startsWith('+')) {
            return `<span class="diff-add">${safe}</span>`;
        } else if (line.startsWith('-')) {
            return `<span class="diff-del">${safe}</span>`;
        } else if (line.startsWith('@@')) {
            return `<span class="diff-hunk">${safe}</span>`;
        }
        return `<span>${safe}</span>`;
    }).join('\n');
}

function showResult(rawText) {
    // Mark done step
    setActiveStep('done');
    STEPS.forEach(s => {
        const el = document.getElementById('step-' + s);
        if (el) el.classList.add('completed');
    });

    const sections = parseResult(rawText);
    const hasParsedContent = sections.plan || sections.diff || sections.feedback;

    if (hasParsedContent) {
        // Structured panels
        document.getElementById('panel-raw').classList.add('hidden');

        // Badge
        const badge = document.getElementById('result-badge');
        if (sections.status.toLowerCase().includes('accept')) {
            badge.textContent = '✅ ' + sections.status;
            badge.className = 'result-badge badge-accept';
        } else if (sections.status.toLowerCase().includes('reject')) {
            badge.textContent = '❌ ' + sections.status;
            badge.className = 'result-badge badge-reject';
        } else {
            badge.textContent = '⚠️ ' + (sections.status || 'Pipeline Complete');
            badge.className = 'result-badge badge-warn';
        }

        // Plan
        const planContent = document.getElementById('plan-content');
        if (sections.plan) {
            planContent.innerHTML = renderMarkdown(sections.plan);
            document.getElementById('panel-plan').classList.remove('hidden');
        } else {
            document.getElementById('panel-plan').classList.add('hidden');
        }

        // Diff
        const diffContent = document.getElementById('diff-content');
        if (sections.diff) {
            diffContent.innerHTML = renderDiff(sections.diff);
            document.getElementById('panel-diff').classList.remove('hidden');
            // Store raw diff for copy
            diffContent.dataset.raw = sections.diff;
        } else {
            document.getElementById('panel-diff').classList.add('hidden');
        }

        // Critic feedback
        const criticContent = document.getElementById('critic-content');
        if (sections.feedback) {
            criticContent.innerHTML = renderMarkdown(sections.feedback);
            document.getElementById('panel-critic').classList.remove('hidden');
        } else {
            document.getElementById('panel-critic').classList.add('hidden');
        }

    } else {
        // Fallback: show raw output
        ['panel-plan', 'panel-diff', 'panel-critic'].forEach(id =>
            document.getElementById(id).classList.add('hidden')
        );
        document.getElementById('result-badge').textContent = 'Pipeline Output';
        document.getElementById('result-badge').className = 'result-badge badge-warn';
        const rawEl = document.getElementById('panel-raw');
        rawEl.classList.remove('hidden');
        document.getElementById('raw-content').textContent = rawText;
    }

    // Hide landing, show result
    progressContainer.classList.add('hidden');
    landing.classList.add('hidden');
    resultSection.classList.remove('hidden');
    resultSection.scrollIntoView({ behavior: 'smooth' });
}

// --- Copy diff ---

copyDiffBtn.addEventListener('click', () => {
    const raw = document.getElementById('diff-content').dataset.raw || '';
    navigator.clipboard.writeText(raw).then(() => {
        copyDiffBtn.textContent = 'Copied!';
        setTimeout(() => { copyDiffBtn.textContent = 'Copy'; }, 2000);
    });
});

// --- Run again ---

runAgainBtn.addEventListener('click', () => {
    resultSection.classList.add('hidden');
    landing.classList.remove('hidden');
    progressContainer.classList.add('hidden');
    topicInput.disabled = false;
    createButton.disabled = false;
    createButton.innerHTML = '<span>Fix Issue</span><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M3 10a.75.75 0 01.75-.75h10.638L10.23 5.29a.75.75 0 111.04-1.08l5.5 5.25a.75.75 0 010 1.08l-5.5 5.25a.75.75 0 11-1.04-1.08l4.158-3.96H3.75A.75.75 0 013 10z" clip-rule="evenodd" /></svg>';
    topicInput.value = '';
    STEPS.forEach(s => {
        const el = document.getElementById('step-' + s);
        if (el) el.classList.remove('active', 'completed');
    });
});

// --- Form submit ---

createForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const topic = topicInput.value.trim();
    if (!topic) return;

    showProgress();
    statusText.textContent = 'Starting pipeline...';

    try {
        const response = await fetch('/api/chat_stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: topic, session_id: sessionId })
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { value, done } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const data = JSON.parse(line);
                    if (data.type === 'progress') {
                        updateStatus(data.text);
                    } else if (data.type === 'result') {
                        showResult(data.text);
                        return;
                    }
                } catch (err) {
                    console.error('Parse error:', err, line);
                }
            }
        }

    } catch (error) {
        console.error('Error:', error);
        statusText.textContent = 'Something went wrong: ' + error.message;
        createButton.disabled = false;
        topicInput.disabled = false;
        createButton.innerHTML = '<span>Fix Issue</span>';
    }
});
