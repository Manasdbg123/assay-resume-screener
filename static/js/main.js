/**
 * Resume Screener — Client-Side Logic
 * Handles drag-and-drop, form submission, results rendering,
 * and all UI animations / transitions.
 */

document.addEventListener('DOMContentLoaded', () => {
    // --- Elements ---
    const form = document.getElementById('analyze-form');
    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('resume-input');
    const fileInfo = document.getElementById('file-info');
    const fileName = document.getElementById('file-name');
    const fileSize = document.getElementById('file-size');
    const fileRemove = document.getElementById('file-remove');
    const jobDescription = document.getElementById('job-description');
    const charCount = document.getElementById('char-count');
    const analyzeBtn = document.getElementById('analyze-btn');
    const btnText = analyzeBtn.querySelector('.btn-text');
    const btnLoader = analyzeBtn.querySelector('.btn-loader');
    const btnArrow = analyzeBtn.querySelector('.btn-arrow');
    const errorToast = document.getElementById('error-toast');
    const errorMessage = document.getElementById('error-message');
    const errorClose = document.getElementById('error-close');
    const uploadSection = document.getElementById('upload-section');
    const resultsSection = document.getElementById('results-section');
    const analyzeAnotherBtn = document.getElementById('analyze-another-btn');
    const progressBar = document.getElementById('progress-bar');

    // Discover-mode elements
    const modeTabs = document.querySelectorAll('.mode-tab');
    const jdCard = document.getElementById('jd-card');
    const discoverCard = document.getElementById('discover-card');
    const jobsSection = document.getElementById('jobs-section');
    const jobsAnotherBtn = document.getElementById('jobs-another-btn');
    const jobIndexNote = document.getElementById('job-index-note');
    const remoteOnly = document.getElementById('remote-only');

    let selectedFile = null;
    // 'check' = CV against one pasted JD (the original flow, unchanged).
    // 'discover' = CV against the indexed job corpus.
    let mode = 'check';

    // --- Utility Functions ---
    function hideError() {
        errorToast.style.display = 'none';
    }

    function showError(message) {
        errorMessage.textContent = message;
        errorToast.style.display = 'flex';
        errorToast.classList.add('shake');
        setTimeout(() => errorToast.classList.remove('shake'), 500);
        setTimeout(() => {
            errorToast.style.display = 'none';
        }, 8000);
    }

    function formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function setLoading(loading) {
        analyzeBtn.disabled = loading;
        btnText.style.display = loading ? 'none' : 'inline';
        btnArrow.style.display = loading ? 'none' : 'inline';
        btnLoader.style.display = loading ? 'inline-flex' : 'none';

        // Progress bar animation
        if (loading) {
            progressBar.classList.add('active');
        } else {
            progressBar.classList.remove('active');
        }
    }

    // --- Mode switching ---
    //
    // 'check' and 'discover' share the upload form; 'browse' needs no CV at all.
    const browseSection = document.getElementById('browse-section');
    const heroCopy = {
        check: ['Evidence-based screening',
                'See how your CV really reads against a job',
                'Every score comes with a quote from your resume behind it, so you know exactly what to fix before you apply.'],
        discover: ['Job discovery',
                   'Upload your CV. Get your best-fit roles.',
                   'We rank thousands of live postings against your experience and explain the strongest matches in detail.'],
    };

    function showOnly(section) {
        [uploadSection, resultsSection, jobsSection, browseSection].forEach((el) => {
            el.style.display = el === section ? 'block' : 'none';
        });
    }

    function setMode(next) {
        mode = next;
        modeTabs.forEach((tab) => {
            const active = tab.dataset.mode === next;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', String(active));
        });
        hideError();
        if (next === 'browse') {
            showOnly(browseSection);
            ensureBrowseLoaded();
            return;
        }
        showOnly(uploadSection);
        const [eyebrow, title, sub] = heroCopy[next];
        document.getElementById('hero-eyebrow').textContent = eyebrow;
        document.getElementById('hero-title').textContent = title;
        document.getElementById('hero-sub').textContent = sub;
        jdCard.style.display = next === 'check' ? '' : 'none';
        discoverCard.style.display = next === 'discover' ? '' : 'none';
        btnText.textContent = next === 'check' ? 'Analyze resume' : 'Find matching jobs';
        document.getElementById('loader-text').textContent =
            next === 'check' ? 'Analyzing… this takes about 15 seconds' : 'Ranking postings…';
        if (next === 'discover') loadJobIndexStatus();
    }

    modeTabs.forEach((tab) => tab.addEventListener('click', () => setMode(tab.dataset.mode)));

    // --- Theme ---
    document.getElementById('theme-toggle').addEventListener('click', () => {
        const root = document.documentElement;
        const current = root.dataset.theme
            || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
        const next = current === 'dark' ? 'light' : 'dark';
        root.dataset.theme = next;
        try { localStorage.setItem('theme', next); } catch (err) { /* storage blocked */ }
    });

    async function loadJobIndexStatus() {
        // Tell the user up front whether anything is indexed - an empty index is
        // a setup step they have to run, not a bug in the search.
        try {
            const res = await fetch('/jobs/stats');
            const stats = await res.json();
            if (!stats.total) {
                jobIndexNote.textContent = stats.refreshing
                    ? 'Fetching live jobs from company boards. Try again in a minute.'
                    : 'No jobs indexed yet. Run: python -m resumescreener.jobs.ingest';
                jobIndexNote.classList.add('warn');
            } else {
                jobIndexNote.textContent =
                    `Searching ${stats.total.toLocaleString()} live postings.`;
                jobIndexNote.classList.remove('warn');
            }
        } catch {
            jobIndexNote.textContent = 'Could not reach the job index.';
            jobIndexNote.classList.add('warn');
        }
    }

    // --- Drop Zone ---
    dropZone.addEventListener('click', () => fileInput.click());

    // Keyboard accessibility: Enter/Space to trigger file browse
    dropZone.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            fileInput.click();
        }
    });

    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('drag-over');
    });

    dropZone.addEventListener('dragleave', () => {
        dropZone.classList.remove('drag-over');
    });

    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            handleFileSelect(files[0]);
        }
    });

    // Page-level drag feedback
    let dragCounter = 0;
    document.addEventListener('dragenter', (e) => {
        e.preventDefault();
        dragCounter++;
        if (dragCounter === 1) {
            document.body.classList.add('file-dragging');
        }
    });

    document.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dragCounter--;
        if (dragCounter === 0) {
            document.body.classList.remove('file-dragging');
        }
    });

    document.addEventListener('drop', (e) => {
        e.preventDefault();
        dragCounter = 0;
        document.body.classList.remove('file-dragging');
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleFileSelect(e.target.files[0]);
        }
    });

    function handleFileSelect(file) {
        const validTypes = ['.pdf', '.docx', '.txt'];
        const ext = '.' + file.name.split('.').pop().toLowerCase();

        if (!validTypes.includes(ext)) {
            showError('Please upload a PDF, DOCX, or TXT file.');
            return;
        }

        if (file.size > 16 * 1024 * 1024) {
            showError('File is too large. Maximum size is 16MB.');
            return;
        }

        selectedFile = file;
        fileName.textContent = file.name;
        fileSize.textContent = formatFileSize(file.size);
        fileInfo.style.display = 'flex';
        dropZone.style.display = 'none';
    }

    fileRemove.addEventListener('click', () => {
        selectedFile = null;
        fileInput.value = '';
        fileInfo.style.display = 'none';
        dropZone.style.display = '';  // Revert to CSS default
    });

    // --- Character Count ---
    jobDescription.addEventListener('input', () => {
        charCount.textContent = jobDescription.value.length;
        jdSource.style.display = 'none';
    });
    const jdSource = document.getElementById('jd-source');

    // --- Error Handling ---
    errorClose.addEventListener('click', () => {
        errorToast.style.display = 'none';
    });

    // --- Form Submit ---
    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        hideError();

        if (!selectedFile) {
            showError('Please upload your resume first.');
            return;
        }

        const formData = new FormData();
        formData.append('resume', selectedFile);

        if (mode === 'check') {
            const jd = jobDescription.value.trim();
            if (!jd) {
                showError('Please enter a job description.');
                return;
            }
            if (jd.length < 40) {
                showError('Job description is too short - please paste at least 40 characters.');
                return;
            }
            formData.append('job_description', jd);
        } else {
            formData.append('remote_only', remoteOnly.checked ? 'true' : 'false');
        }

        setLoading(true);
        const endpoint = mode === 'check' ? '/analyze' : '/match-jobs';

        try {
            const response = await fetch(endpoint, { method: 'POST', body: formData });
            const data = await response.json();

            if (!response.ok) {
                showError(data.error || 'Something went wrong. Please try again.');
                setLoading(false);
                return;
            }

            setTimeout(() => {
                setLoading(false);
                if (mode === 'check') {
                    renderResults(data);
                } else {
                    renderJobMatches(data);
                }
            }, 300);
        } catch (err) {
            showError('Network error. Please check your connection and try again.');
            setLoading(false);
        }
    });

    // --- Render Results ---
    //
    // The API returns { result, engine, model, degraded, ... }. Everything inside
    // `result` is model-authored text, so it is written with textContent, never
    // innerHTML - an LLM response is untrusted input like any other.

    function renderResults(payload) {
        const r = payload.result;

        uploadSection.classList.add('section-exit');
        setTimeout(() => {
            uploadSection.style.display = 'none';
            uploadSection.classList.remove('section-exit');
            resultsSection.style.display = 'block';
            resultsSection.classList.add('section-enter');
            window.scrollTo({ top: 0, behavior: 'smooth' });

            document.getElementById('results-filename').textContent =
                `Analyzed: ${payload.filename || 'resume'}`;

            renderVerdict(payload, r);

            addScoreGradient(r.overall_score);
            animateScore(r.overall_score);

            const label = document.getElementById('score-label');
            const bands = [
                [80, 'Excellent Match', 'score-excellent'],
                [65, 'Good Match', 'score-good'],
                [45, 'Moderate Match', 'score-moderate'],
                [0, 'Needs Improvement', 'score-low'],
            ];
            const [, text, cls] = bands.find(([min]) => r.overall_score >= min);
            label.textContent = text;
            label.className = `score-label ${cls}`;

            setTimeout(() => {
                setBarScore('skills', r.skills.score, r.skills.reasoning);
                setBarScore('domain', r.domain_relevance.score, r.domain_relevance.reasoning);
                setBarScore('experience', r.experience.score, r.experience.reasoning);
                setBarScore('education', r.education.score, r.education.reasoning);
            }, 300);

            renderSkillTags('matched-skills', r.matched_skills, 'matched', 'matched-count');
            renderSkillTags('missing-skills', r.missing_skills, 'missing', 'missing-count');
            renderSkillTags('transferable-skills', r.transferable_skills, 'extra', 'transferable-count');

            renderEvidence(r.skill_evidence);
            renderFlags(r.red_flags);
            renderSuggestions(r.suggestions);

            setTimeout(() => resultsSection.classList.remove('section-enter'), 700);
        }, 400);
    }

    function renderVerdict(payload, result) {
        const verdictLabels = {
            strong_match: 'Strong Match',
            match: 'Match',
            partial_match: 'Partial Match',
            weak_match: 'Weak Match',
        };

        const badge = document.getElementById('verdict-badge');
        badge.textContent = verdictLabels[result.verdict] || result.verdict;
        badge.className = `verdict-badge verdict-${result.verdict}`;

        const engine = document.getElementById('engine-badge');
        // Branch on the baseline, not on a provider name: an unrecognised
        // provider must never be mislabelled as keyword scoring.
        engine.textContent = payload.engine === 'baseline'
            ? 'Keyword baseline'
            : `Analyzed by ${payload.model || payload.engine}`;
        engine.className = `engine-badge engine-${payload.engine}`;

        document.getElementById('verdict-summary').textContent = result.summary;
        document.getElementById('degraded-notice').style.display =
            payload.degraded ? 'block' : 'none';
    }

    function renderEvidence(evidence) {
        const container = document.getElementById('evidence-list');
        container.innerHTML = '';

        if (!evidence || evidence.length === 0) {
            container.appendChild(noItems('No requirements were identified'));
            return;
        }

        // Present first, so the reader sees what the candidate has before the gaps.
        const ordered = [...evidence].sort((a, b) => Number(b.present) - Number(a.present));

        ordered.forEach((item) => {
            const row = document.createElement('div');
            row.className = `evidence-row ${item.present ? 'present' : 'absent'}`;

            const head = document.createElement('div');
            head.className = 'evidence-head';

            const name = document.createElement('span');
            name.className = 'evidence-skill';
            name.textContent = item.skill;

            const strength = document.createElement('span');
            strength.className = `evidence-strength strength-${item.strength}`;
            strength.textContent = item.present ? item.strength : 'not found';

            head.append(name, strength);
            row.appendChild(head);

            if (item.evidence) {
                const quote = document.createElement('p');
                quote.className = 'evidence-quote';
                quote.textContent = item.evidence;
                row.appendChild(quote);
            }
            container.appendChild(row);
        });
    }

    function renderFlags(flags) {
        const card = document.getElementById('flags-card');
        const container = document.getElementById('flags-list');
        container.innerHTML = '';

        if (!flags || flags.length === 0) {
            card.style.display = 'none';
            return;
        }
        card.style.display = 'block';
        flags.forEach((flag) => {
            const item = document.createElement('div');
            item.className = 'flag-item';
            item.textContent = flag;
            container.appendChild(item);
        });
    }

    function noItems(text) {
        const span = document.createElement('span');
        span.className = 'no-items';
        span.textContent = text;
        return span;
    }

    // --- Render Job Matches (discover mode) ---
    //
    // Same rule as the single-JD view: every string here originates from a job
    // board or the model, so it is written with textContent, never innerHTML.

    function renderJobMatches(payload) {
        uploadSection.classList.add('section-exit');
        setTimeout(() => {
            uploadSection.style.display = 'none';
            uploadSection.classList.remove('section-exit');
            jobsSection.style.display = 'block';
            jobsSection.classList.add('section-enter');
            window.scrollTo({ top: 0, behavior: 'smooth' });

            document.getElementById('jobs-filename').textContent =
                `Matched against: ${payload.filename || 'your resume'}`;

            const summary = document.getElementById('jobs-summary');
            summary.textContent =
                `${payload.matches.length} matches from ` +
                `${payload.total_candidates_considered.toLocaleString()} postings \u00b7 ` +
                `${payload.llm_scored_count} scored in detail by ` +
                `${payload.engine === 'baseline' ? 'keyword matching' : payload.engine}`;

            // Say which stage degraded, not just that something did. "The AI was
            // unavailable" is wrong when the AI scored jobs fine and it was the
            // embedding ranker that fell back.
            const degradedBox = document.getElementById('jobs-degraded');
            degradedBox.style.display = payload.degraded ? 'block' : 'none';
            if (payload.degraded) {
                const reasons = [];
                if (payload.ranker === 'tfidf') {
                    reasons.push('Ranking fell back to keyword overlap because the ' +
                                 'semantic search was unavailable, so the ordering is rougher than usual.');
                }
                if (!payload.llm_scored_count) {
                    reasons.push('No job could be scored in detail, so every score below ' +
                                 'is keyword-based.');
                } else if (payload.llm_scored_count < 3) {
                    reasons.push(`Only ${payload.llm_scored_count} job(s) could be scored in ` +
                                 'detail before the AI quota ran out.');
                }
                document.getElementById('jobs-degraded-detail').textContent =
                    reasons.join(' ') || 'Some results were produced by the fallback engine.';
            }

            const list = document.getElementById('jobs-list');
            list.innerHTML = '';

            if (!payload.matches.length) {
                list.appendChild(noItems('No matching jobs found. Try widening your filters.'));
            }
            payload.matches.forEach((match, i) => list.appendChild(buildJobCard(match, i)));

            setTimeout(() => jobsSection.classList.remove('section-enter'), 700);
        }, 400);
    }

    function buildJobCard(match, index) {
        const job = match.job;
        const card = document.createElement('article');
        card.className = 'job-card';
        card.style.animationDelay = `${Math.min(index * 0.05, 0.8)}s`;

        // --- score ---
        const score = document.createElement('div');
        score.className = 'job-score';
        const band = match.match_score >= 80 ? 'strong'
                   : match.match_score >= 65 ? 'good'
                   : match.match_score >= 45 ? 'fair' : 'weak';
        score.classList.add(`job-score-${band}`);
        score.textContent = Math.round(match.match_score);

        // --- header ---
        const body = document.createElement('div');
        body.className = 'job-body';

        const title = document.createElement('h3');
        title.className = 'job-title';
        if (/^https?:\/\//i.test(job.url || '')) {
            const link = document.createElement('a');
            link.href = job.url;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';   // never hand the opener window over
            link.textContent = job.title;
            title.appendChild(link);
        } else {
            title.textContent = job.title;
        }

        const meta = document.createElement('p');
        meta.className = 'job-meta';
        meta.textContent = [job.company, job.location].filter(Boolean).join(' \u00b7 ');

        const badges = document.createElement('div');
        badges.className = 'job-badges';

        // Provenance again: a detailed LLM judgement and a keyword tally must
        // never look the same to someone deciding where to apply.
        const how = document.createElement('span');
        how.className = `job-badge job-badge-${match.scored_by}`;
        how.textContent = match.scored_by === 'llm' ? 'Scored in detail' : 'Keyword ranked';
        badges.appendChild(how);

        if (job.remote) {
            const remote = document.createElement('span');
            remote.className = 'job-badge job-badge-remote';
            remote.textContent = 'Remote';
            badges.appendChild(remote);
        }
        const source = document.createElement('span');
        source.className = 'job-badge job-badge-source';
        source.textContent = job.source;
        badges.appendChild(source);

        body.append(title, meta, badges);

        if (match.result && match.result.summary) {
            const summary = document.createElement('p');
            summary.className = 'job-summary';
            summary.textContent = match.result.summary;
            body.appendChild(summary);
        }

        if (match.result) {
            body.appendChild(buildJobDetails(match.result));
        }

        card.append(score, body);
        return card;
    }

    function buildJobDetails(result) {
        const details = document.createElement('details');
        details.className = 'job-details';

        const summary = document.createElement('summary');
        summary.textContent = 'Why this match';
        details.appendChild(summary);

        const matched = (result.matched_skills || []).slice(0, 10);
        const missing = (result.missing_skills || []).slice(0, 10);

        [['Matched', matched, 'matched'], ['Missing', missing, 'missing']].forEach(
            ([label, items, cls]) => {
                if (!items.length) return;
                const row = document.createElement('div');
                row.className = 'job-skill-row';

                const heading = document.createElement('span');
                heading.className = 'job-skill-label';
                heading.textContent = label;
                row.appendChild(heading);

                items.forEach((skill) => {
                    const tag = document.createElement('span');
                    tag.className = `skill-tag ${cls}`;
                    tag.textContent = skill;
                    row.appendChild(tag);
                });
                details.appendChild(row);
            },
        );

        (result.skill_evidence || []).filter((e) => e.present).slice(0, 3).forEach((e) => {
            if (!e.evidence) return;
            const quote = document.createElement('p');
            quote.className = 'job-evidence';
            quote.textContent = `${e.skill}: ${e.evidence}`;
            details.appendChild(quote);
        });

        return details;
    }

    jobsAnotherBtn.addEventListener('click', () => {
        jobsSection.classList.add('section-exit');
        setTimeout(() => {
            jobsSection.style.display = 'none';
            jobsSection.classList.remove('section-exit');
            uploadSection.style.display = 'block';
            uploadSection.classList.add('section-enter');
            window.scrollTo({ top: 0, behavior: 'smooth' });
            setTimeout(() => uploadSection.classList.remove('section-enter'), 700);
        }, 400);
    });

    function addScoreGradient(score) {
        const existing = document.getElementById('score-gradient-defs');
        if (existing) existing.remove();

        const svg = document.querySelector('.score-ring');
        const NS = 'http://www.w3.org/2000/svg';

        const bands = [
            [80, '#10b981', '#14b8a6'],
            [60, '#6366f1', '#8b5cf6'],
            [40, '#f59e0b', '#f97316'],
            [0,  '#f43f5e', '#e11d48'],
        ];
        const [, color1, color2] = bands.find(([min]) => score >= min);

        // Built with DOM calls rather than innerHTML. Nothing here is user data,
        // but keeping the rule absolute means a reviewer never has to check.
        const defs = document.createElementNS(NS, 'defs');
        defs.id = 'score-gradient-defs';

        const gradient = document.createElementNS(NS, 'linearGradient');
        gradient.setAttribute('id', 'scoreGradient');
        gradient.setAttribute('x1', '0%');
        gradient.setAttribute('y1', '0%');
        gradient.setAttribute('x2', '100%');
        gradient.setAttribute('y2', '100%');

        [[0, color1], [100, color2]].forEach(([offset, color]) => {
            const stop = document.createElementNS(NS, 'stop');
            stop.setAttribute('offset', `${offset}%`);
            stop.setAttribute('stop-color', color);
            gradient.appendChild(stop);
        });

        defs.appendChild(gradient);
        svg.insertBefore(defs, svg.firstChild);

        document.getElementById('score-ring-fill').setAttribute('stroke', 'url(#scoreGradient)');
    }

    function animateScore(target) {
        const scoreEl = document.getElementById('overall-score');
        const ringFill = document.getElementById('score-ring-fill');
        const circumference = 2 * Math.PI * 95; // r=95

        // Handle edge case: score is 0
        if (target <= 0) {
            scoreEl.textContent = '0';
            ringFill.style.strokeDashoffset = circumference;
            return;
        }

        // Animate ring
        const offset = circumference - (target / 100) * circumference;
        setTimeout(() => {
            ringFill.style.strokeDashoffset = offset;
        }, 100);

        // Animate number
        let current = 0;
        const duration = 1500;
        const step = target / (duration / 16);

        function tick() {
            current += step;
            if (current >= target) {
                scoreEl.textContent = Math.round(target);
                return;
            }
            scoreEl.textContent = Math.round(current);
            requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);
    }

    function setBarScore(category, score, reasoning) {
        const bar = document.getElementById(`${category}-bar`);
        const value = document.getElementById(`${category}-score`);
        bar.style.width = `${Math.min(score, 100)}%`;
        value.textContent = `${Math.round(score)}%`;

        // The reasoning is why the score is what it is - surface it on hover.
        if (reasoning) {
            const card = bar.closest('.score-card');
            if (card) card.title = reasoning;
        }
    }

    function renderSkillTags(containerId, skills, className, countId) {
        const container = document.getElementById(containerId);
        const countEl = document.getElementById(countId);

        container.innerHTML = '';
        countEl.textContent = skills.length;

        if (!skills || skills.length === 0) {
            container.appendChild(noItems('None detected'));
            return;
        }

        skills.forEach((skill, i) => {
            const tag = document.createElement('span');
            tag.className = `skill-tag ${className}`;
            tag.textContent = skill;
            tag.style.animationDelay = `${0.6 + i * 0.05}s`;
            container.appendChild(tag);
        });
    }

    function renderSuggestions(suggestions) {
        const container = document.getElementById('suggestions-list');
        container.innerHTML = '';

        const priorityIcons = { high: '\ud83d\udd34', medium: '\ud83d\udfe1', low: '\ud83d\udfe2' };
        const order = { high: 0, medium: 1, low: 2 };
        const sorted = [...(suggestions || [])].sort(
            (a, b) => (order[a.priority] ?? 3) - (order[b.priority] ?? 3)
        );

        sorted.forEach((s, i) => {
            const item = document.createElement('div');
            item.className = `suggestion-item ${s.priority}`;
            item.style.animationDelay = `${0.8 + i * 0.1}s`;

            const tag = document.createElement('span');
            tag.className = 'suggestion-priority';
            tag.textContent = `${priorityIcons[s.priority] || ''} ${s.priority}`;

            const message = document.createElement('span');
            message.className = 'suggestion-message';
            message.textContent = s.message;   // model output - never innerHTML

            item.append(tag, message);
            container.appendChild(item);
        });
    }

    // --- Analyze Another ---
    analyzeAnotherBtn.addEventListener('click', () => {
        resultsSection.classList.add('section-exit');
        setTimeout(() => {
            resultsSection.style.display = 'none';
            resultsSection.classList.remove('section-exit');
            uploadSection.style.display = 'block';
            uploadSection.classList.add('section-enter');

            // Reset form
            selectedFile = null;
            fileInput.value = '';
            fileInfo.style.display = 'none';
            dropZone.style.display = '';  // Revert to CSS default
            jobDescription.value = '';
            charCount.textContent = '0';
            jdSource.style.display = 'none';

            // Reset score ring
            document.getElementById('score-ring-fill').style.strokeDashoffset = '597';
            document.getElementById('overall-score').textContent = '0';

            window.scrollTo({ top: 0, behavior: 'smooth' });

            setTimeout(() => {
                uploadSection.classList.remove('section-enter');
            }, 700);
        }, 400);
    });

    // =====================================================================
    // Browse jobs
    //
    // No CV needed: search the index, or look up any company live. Every
    // string here comes from a job board, so it is written with textContent
    // and links are only followed when they are plain http(s).
    // =====================================================================

    const browseList = document.getElementById('browse-list');
    const browseCount = document.getElementById('browse-count');
    const browseMore = document.getElementById('browse-more');
    const browseStats = document.getElementById('browse-stats');
    const companyList = document.getElementById('company-list');
    const companyCount = document.getElementById('company-count');
    const companyFilter = document.getElementById('company-filter');
    const activeFilters = document.getElementById('active-filters');
    const sourceSelect = document.getElementById('browse-source');
    const lookupForm = document.getElementById('lookup-form');
    const lookupInput = document.getElementById('lookup-input');
    const lookupBtn = document.getElementById('lookup-btn');
    const lookupResult = document.getElementById('lookup-result');

    const browse = {
        loaded: false, page: 1, company: '', requestId: 0,
    };

    const boardNames = {
        greenhouse: 'Greenhouse', lever: 'Lever', ashby: 'Ashby', workable: 'Workable',
        smartrecruiters: 'SmartRecruiters', recruitee: 'Recruitee',
        remotive: 'Remotive', arbeitnow: 'Arbeitnow',
    };

    function safeUrl(url) {
        return /^https?:\/\//i.test(url || '') ? url : null;
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function relativeTime(iso) {
        if (!iso) return '';
        const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
        if (Number.isNaN(days)) return '';
        if (days <= 0) return 'Today';
        if (days === 1) return 'Yesterday';
        if (days < 7) return `${days} days ago`;
        if (days < 30) return `${Math.floor(days / 7)} wk ago`;
        if (days < 365) return `${Math.floor(days / 30)} mo ago`;
        return `${Math.floor(days / 365)} yr ago`;
    }

    function avatar(name) {
        // A stable colour per company, so the same logo-less company always
        // looks the same across the list and the drawer.
        const node = el('span', 'avatar', (name || '?').trim().charAt(0).toUpperCase());
        let hash = 0;
        for (const ch of name || '') hash = (hash * 31 + ch.charCodeAt(0)) | 0;
        node.style.setProperty('--avatar-hue', String(Math.abs(hash) % 360));
        return node;
    }

    function debounce(fn, ms) {
        let timer;
        return (...args) => {
            clearTimeout(timer);
            timer = setTimeout(() => fn(...args), ms);
        };
    }

    async function ensureBrowseLoaded() {
        if (browse.loaded) return;
        browse.loaded = true;
        await loadBrowseStats();
        loadCompanies();
        runSearch(true);
    }

    async function loadBrowseStats() {
        try {
            const stats = await (await fetch('/jobs/stats')).json();
            browse.refreshing = Boolean(stats.refreshing);
            browseStats.textContent = '';
            [
                [stats.total, 'open roles'],
                [stats.companies, 'companies'],
                [stats.remote, 'remote'],
            ].forEach(([value, label]) => {
                const stat = el('div', 'stat');
                stat.append(el('span', 'stat-value', (value || 0).toLocaleString()),
                            el('span', 'stat-label', label));
                browseStats.appendChild(stat);
            });
            const current = sourceSelect.value;
            while (sourceSelect.options.length > 1) sourceSelect.remove(1);
            Object.entries(stats.by_source || {})
                .sort((a, b) => b[1] - a[1])
                .forEach(([source, count]) => {
                    const option = el('option', null,
                        `${boardNames[source] || source} (${count.toLocaleString()})`);
                    option.value = source;
                    sourceSelect.appendChild(option);
                });
            sourceSelect.value = current;
        } catch {
            browseStats.textContent = '';
        }
    }

    async function loadCompanies() {
        const params = new URLSearchParams({ q: companyFilter.value.trim(), limit: '80' });
        try {
            const data = await (await fetch(`/jobs/companies?${params}`)).json();
            companyList.innerHTML = '';
            companyCount.textContent = data.companies.length ? `${data.companies.length}+` : '';
            if (!data.companies.length) {
                companyList.appendChild(el('li', 'no-items',
                    companyFilter.value ? 'No company matches. Try "Find a company" above.'
                                        : 'No companies indexed yet.'));
            }
            data.companies.forEach(({ company, jobs }) => {
                const item = el('li');
                const button = el('button', 'company-item');
                button.type = 'button';
                button.classList.toggle('active', company === browse.company);
                button.append(avatar(company), el('span', 'company-name', company),
                              el('span', 'company-jobs', jobs.toLocaleString()));
                button.addEventListener('click', () => {
                    browse.company = browse.company === company ? '' : company;
                    loadCompanies();
                    runSearch(true);
                });
                item.appendChild(button);
                companyList.appendChild(item);
            });
        } catch {
            companyList.innerHTML = '';
        }
    }

    function browseParams() {
        const params = new URLSearchParams({ page: String(browse.page), per_page: '24' });
        const add = (key, value) => { if (value) params.set(key, value); };
        add('q', document.getElementById('browse-query').value.trim());
        add('location', document.getElementById('browse-location').value.trim());
        add('days', document.getElementById('browse-days').value);
        add('source', sourceSelect.value);
        add('sort', document.getElementById('browse-sort').value);
        add('company', browse.company);
        if (document.getElementById('browse-remote').checked) params.set('remote', 'true');
        return params;
    }

    function renderActiveFilters() {
        activeFilters.innerHTML = '';
        if (!browse.company) return;
        const chip = el('button', 'filter-chip');
        chip.type = 'button';
        chip.append(el('span', null, `Company: ${browse.company}`), el('span', 'chip-x', '×'));
        chip.setAttribute('aria-label', `Remove company filter ${browse.company}`);
        chip.addEventListener('click', () => {
            browse.company = '';
            loadCompanies();
            runSearch(true);
        });
        activeFilters.appendChild(chip);
    }

    function skeletons(count) {
        for (let i = 0; i < count; i++) {
            const card = el('div', 'browse-card skeleton');
            card.append(el('div', 'sk sk-avatar'), el('div', 'sk sk-line'),
                        el('div', 'sk sk-line short'));
            browseList.appendChild(card);
        }
    }

    async function runSearch(reset) {
        if (reset) {
            browse.page = 1;
            browseList.innerHTML = '';
            skeletons(6);
        }
        renderActiveFilters();
        const requestId = ++browse.requestId;
        browseMore.disabled = true;

        try {
            const res = await fetch(`/jobs?${browseParams()}`);
            const data = await res.json();
            if (requestId !== browse.requestId) return;   // a newer search superseded this one
            if (!res.ok) throw new Error(data.error || 'Search failed');

            if (reset) browseList.innerHTML = '';
            data.jobs.forEach((job, i) => browseList.appendChild(buildBrowseCard(job, i)));

            if (!data.total) {
                const empty = el('div', 'empty-state');
                if (browse.refreshing && !browseParams().has('q') && !browse.company) {
                    empty.append(
                        el('h3', null, 'Fetching live jobs…'),
                        el('p', null, 'The server is pulling current openings from company job boards. ' +
                           'This takes a minute or two after a restart. Reload the page shortly.'),
                    );
                } else {
                    empty.append(
                        el('h3', null, 'No roles match these filters'),
                        el('p', null, 'Loosen a filter, or use "Find a company" above to pull a company’s openings live.'),
                    );
                }
                browseList.appendChild(empty);
            }
            const shown = Math.min(data.page * data.per_page, data.total);
            browseCount.textContent = data.total
                ? `Showing ${shown.toLocaleString()} of ${data.total.toLocaleString()} roles`
                : '';
            browseMore.style.display = data.has_more ? '' : 'none';
        } catch (err) {
            if (requestId !== browse.requestId) return;
            browseList.innerHTML = '';
            showError('Could not load jobs. Please try again.');
        } finally {
            browseMore.disabled = false;
        }
    }

    function buildBrowseCard(job, index) {
        const card = el('article', 'browse-card');
        card.tabIndex = 0;
        card.setAttribute('role', 'button');
        card.setAttribute('aria-label', `${job.title} at ${job.company}`);
        card.style.animationDelay = `${Math.min(index * 0.03, 0.4)}s`;

        const head = el('div', 'browse-card-head');
        const titles = el('div', 'browse-card-titles');
        titles.append(el('h3', 'browse-title', job.title), el('p', 'browse-company', job.company));
        head.append(avatar(job.company), titles);
        const posted = relativeTime(job.posted_at);
        if (posted) head.appendChild(el('span', 'browse-posted', posted));

        const meta = el('div', 'browse-meta');
        if (job.location) meta.appendChild(el('span', 'meta-item meta-location', job.location));
        if (job.remote) meta.appendChild(el('span', 'pill pill-remote', 'Remote'));
        meta.appendChild(el('span', 'pill pill-source', boardNames[job.source] || job.source));
        (job.tags || []).slice(0, 2).forEach((tag) => meta.appendChild(el('span', 'pill', tag)));

        card.append(head, meta, el('p', 'browse-snippet', job.snippet));

        const open = () => openDrawer(job.id);
        card.addEventListener('click', open);
        card.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
        });
        return card;
    }

    const rerun = debounce(() => runSearch(true), 300);
    ['browse-query', 'browse-location'].forEach((id) =>
        document.getElementById(id).addEventListener('input', rerun));
    ['browse-days', 'browse-source', 'browse-sort', 'browse-remote'].forEach((id) =>
        document.getElementById(id).addEventListener('change', () => runSearch(true)));
    companyFilter.addEventListener('input', debounce(loadCompanies, 250));
    browseMore.addEventListener('click', () => {
        browse.page += 1;
        runSearch(false);
    });

    // --- Company lookup: any company, straight from its job board ---
    lookupForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const company = lookupInput.value.trim();
        if (company.length < 2) {
            showError('Enter a company name or a careers-page URL.');
            return;
        }
        lookupBtn.disabled = true;
        lookupBtn.querySelector('.btn-label').textContent = 'Searching boards…';
        lookupBtn.querySelector('.spinner').style.display = 'inline-block';
        progressBar.classList.add('active');

        try {
            const res = await fetch('/jobs/lookup', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ company }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || 'Lookup failed.');

            lookupResult.style.display = 'flex';
            lookupResult.textContent = '';
            lookupResult.className = `lookup-result ${data.found ? 'found' : 'missing'}`;
            if (data.found) {
                const boards = data.boards.map((b) => boardNames[b.ats] || b.ats).join(', ');
                lookupResult.append(
                    el('strong', null, `${data.total.toLocaleString()} open roles at ${data.company}`),
                    el('span', null, `Live from ${boards}${data.cached ? ' · checked recently' : ''}`),
                );
                browse.company = data.company;
                loadBrowseStats();
                loadCompanies();
                runSearch(true);
            } else {
                lookupResult.append(
                    el('strong', null, `No open roles found for “${company}”`),
                    el('span', null, 'We checked Greenhouse, Lever, Ashby, Workable, SmartRecruiters and ' +
                       'Recruitee. Try the exact company name, or paste the URL of its careers page.'),
                );
            }
        } catch (err) {
            showError(err.message || 'Lookup failed. Please try again.');
        } finally {
            lookupBtn.disabled = false;
            lookupBtn.querySelector('.btn-label').textContent = 'Get open roles';
            lookupBtn.querySelector('.spinner').style.display = 'none';
            progressBar.classList.remove('active');
        }
    });

    // --- Job detail drawer ---
    const drawer = document.getElementById('job-drawer');
    const drawerBackdrop = document.getElementById('drawer-backdrop');
    let drawerJob = null;
    let lastFocus = null;

    async function openDrawer(jobId) {
        lastFocus = document.activeElement;
        drawerJob = null;
        document.getElementById('drawer-title').textContent = 'Loading…';
        document.getElementById('drawer-company').textContent = '';
        document.getElementById('drawer-meta').innerHTML = '';
        document.getElementById('drawer-body').textContent = '';
        drawerBackdrop.hidden = false;
        drawer.classList.add('open');
        drawer.setAttribute('aria-hidden', 'false');
        document.body.classList.add('drawer-open');
        document.getElementById('drawer-close').focus();

        try {
            const res = await fetch(`/jobs/detail?${new URLSearchParams({ id: jobId })}`);
            const job = await res.json();
            if (!res.ok) throw new Error(job.error);
            drawerJob = job;

            document.getElementById('drawer-title').textContent = job.title;
            document.getElementById('drawer-company').textContent = job.company;
            const avatarSlot = document.getElementById('drawer-avatar');
            const fresh = avatar(job.company);
            avatarSlot.textContent = fresh.textContent;
            avatarSlot.style.setProperty('--avatar-hue', fresh.style.getPropertyValue('--avatar-hue'));

            const meta = document.getElementById('drawer-meta');
            if (job.location) meta.appendChild(el('span', 'meta-item meta-location', job.location));
            if (job.remote) meta.appendChild(el('span', 'pill pill-remote', 'Remote'));
            meta.appendChild(el('span', 'pill pill-source', boardNames[job.source] || job.source));
            const posted = relativeTime(job.posted_at);
            if (posted) meta.appendChild(el('span', 'pill', `Posted ${posted.toLowerCase()}`));
            (job.tags || []).slice(0, 4).forEach((tag) => meta.appendChild(el('span', 'pill', tag)));

            const apply = document.getElementById('drawer-apply');
            const url = safeUrl(job.url);
            apply.style.display = url ? '' : 'none';
            if (url) apply.href = url;

            document.getElementById('drawer-body').textContent = job.description;
        } catch (err) {
            document.getElementById('drawer-title').textContent = 'This posting is no longer available';
        }
    }

    function closeDrawer() {
        drawer.classList.remove('open');
        drawer.setAttribute('aria-hidden', 'true');
        drawerBackdrop.hidden = true;
        document.body.classList.remove('drawer-open');
        if (lastFocus) lastFocus.focus();
    }

    document.getElementById('drawer-close').addEventListener('click', closeDrawer);
    drawerBackdrop.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && drawer.classList.contains('open')) closeDrawer();
    });

    // One click from any posting to a full evidence-backed screening of it.
    document.getElementById('drawer-check').addEventListener('click', () => {
        if (!drawerJob) return;
        const job = drawerJob;
        jobDescription.value = [
            job.title, [job.company, job.location].filter(Boolean).join(' · '), '', job.description,
        ].join('\n');
        charCount.textContent = jobDescription.value.length;
        closeDrawer();
        setMode('check');
        jdSource.textContent = `Loaded: ${job.title} at ${job.company}`;
        jdSource.style.display = 'block';
        window.scrollTo({ top: 0, behavior: 'smooth' });
        if (!selectedFile) dropZone.focus();
    });
});
