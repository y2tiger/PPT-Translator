// State
let currentFileId = null;
let statusPollInterval = null;

// DOM Elements
const uploadArea = document.getElementById('upload-area');
const fileInput = document.getElementById('file-input');
const fileInfo = document.getElementById('file-info');
const optionsSection = document.getElementById('options-section');
const progressSection = document.getElementById('progress-section');
const resultSection = document.getElementById('result-section');
const errorSection = document.getElementById('error-section');
const sourceLangSelect = document.getElementById('source-lang');
const targetLangSelect = document.getElementById('target-lang');

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    loadLanguages();
    setupUploadHandlers();
});

// Load available languages
async function loadLanguages() {
    try {
        const response = await fetch('/api/languages');
        const data = await response.json();

        data.languages.forEach(lang => {
            sourceLangSelect.innerHTML += `<option value="${lang.code}">${lang.name}</option>`;
            targetLangSelect.innerHTML += `<option value="${lang.code}">${lang.name}</option>`;
        });

        // Set defaults
        sourceLangSelect.value = 'en';
        targetLangSelect.value = 'ko';
    } catch (error) {
        console.error('Failed to load languages:', error);
    }
}

// Setup upload handlers
function setupUploadHandlers() {
    uploadArea.addEventListener('click', () => fileInput.click());

    uploadArea.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadArea.classList.add('dragover');
    });

    uploadArea.addEventListener('dragleave', () => {
        uploadArea.classList.remove('dragover');
    });

    uploadArea.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadArea.classList.remove('dragover');
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            handleFileSelect(files[0]);
        }
    });

    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleFileSelect(e.target.files[0]);
        }
    });
}

// Handle file selection
async function handleFileSelect(file) {
    if (!file.name.match(/\.pptx?$/i)) {
        showError('Only .pptx and .ppt files are supported');
        return;
    }

    const formData = new FormData();
    formData.append('file', file);

    try {
        uploadArea.innerHTML = '<div class="upload-icon">⏳</div><p>Uploading...</p>';

        const response = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Upload failed');
        }

        const data = await response.json();
        currentFileId = data.file_id;

        // Show file info
        uploadArea.classList.add('hidden');
        fileInfo.classList.remove('hidden');
        fileInfo.querySelector('.file-name').textContent = data.filename;
        fileInfo.querySelector('.file-stats').textContent =
            `${data.slide_count} slides, ${data.text_count} text elements`;

        // Show options
        optionsSection.classList.remove('hidden');

    } catch (error) {
        resetUploadArea();
        showError(error.message);
    }
}

// Remove file
function removeFile() {
    if (currentFileId) {
        fetch(`/api/file/${currentFileId}`, { method: 'DELETE' }).catch(() => { });
    }
    currentFileId = null;
    resetUploadArea();
    optionsSection.classList.add('hidden');
}

// Reset upload area
function resetUploadArea() {
    uploadArea.classList.remove('hidden');
    uploadArea.innerHTML = `
        <div class="upload-icon">📄</div>
        <p>파일을 드래그하거나 클릭하여 선택</p>
        <p class="hint">.pptx 파일만 지원</p>
    `;
    fileInfo.classList.add('hidden');
    fileInput.value = '';
}

// Start translation
async function startTranslation() {
    const sourceLang = sourceLangSelect.value;
    const targetLang = targetLangSelect.value;
    const reviewLoops = document.getElementById('review-loops').value;

    if (!sourceLang || !targetLang) {
        showError('Please select both source and target languages');
        return;
    }

    if (sourceLang === targetLang) {
        showError('Source and target languages must be different');
        return;
    }

    const formData = new FormData();
    formData.append('source_language', sourceLang);
    formData.append('target_language', targetLang);
    formData.append('min_review_loops', reviewLoops);

    try {
        const response = await fetch(`/api/translate/${currentFileId}`, {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Failed to start translation');
        }

        // Show progress section
        optionsSection.classList.add('hidden');
        progressSection.classList.remove('hidden');
        clearActivityLog();
        addActivityLog('Translation started');

        // Start polling for status
        startStatusPolling();

    } catch (error) {
        showError(error.message);
    }
}

// Status polling
function startStatusPolling() {
    statusPollInterval = setInterval(async () => {
        try {
            const response = await fetch(`/api/status/${currentFileId}`);
            if (!response.ok) {
                throw new Error('Failed to get status');
            }

            const status = await response.json();
            updateProgress(status);

            if (status.status === 'completed') {
                stopStatusPolling();
                showResult(status);
            } else if (status.status === 'error') {
                stopStatusPolling();
                showError(status.message);
            }
        } catch (error) {
            console.error('Status poll error:', error);
        }
    }, 1000);
}

function stopStatusPolling() {
    if (statusPollInterval) {
        clearInterval(statusPollInterval);
        statusPollInterval = null;
    }
}

// Update progress UI
function updateProgress(status) {
    const progressFill = document.getElementById('progress-fill');
    const progressText = document.getElementById('progress-text');
    const statusMessage = document.getElementById('status-message');
    const currentItem = document.getElementById('current-item');
    const reviewRound = document.getElementById('review-round');

    progressFill.style.width = `${status.progress}%`;
    progressText.textContent = `${status.progress}%`;
    statusMessage.textContent = status.message;
    currentItem.textContent = `${status.current_slide} / ${status.total_slides || '-'}`;
    reviewRound.textContent = status.review_loop > 0 ? `Round ${status.review_loop}` : '-';

    // Add to activity log
    if (status.message && status.message !== lastLogMessage) {
        addActivityLog(status.message);
        lastLogMessage = status.message;
    }
}

let lastLogMessage = '';

// Activity log
function addActivityLog(message) {
    const log = document.getElementById('activity-log');
    const time = new Date().toLocaleTimeString();
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = `<span class="log-time">[${time}]</span> ${message}`;
    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;
}

function clearActivityLog() {
    document.getElementById('activity-log').innerHTML = '';
    lastLogMessage = '';
}

// Show result
function showResult(status) {
    progressSection.classList.add('hidden');
    resultSection.classList.remove('hidden');

    const stats = document.getElementById('result-stats');
    stats.innerHTML = `
        <p>Total slides: ${status.total_slides}</p>
        <p>Review iterations completed: ${status.review_loop}</p>
    `;
}

// Download file
function downloadFile() {
    if (currentFileId) {
        window.location.href = `/api/download/${currentFileId}`;
    }
}

// Show error
function showError(message) {
    document.getElementById('upload-section').classList.add('hidden');
    optionsSection.classList.add('hidden');
    progressSection.classList.add('hidden');
    resultSection.classList.add('hidden');
    errorSection.classList.remove('hidden');
    document.getElementById('error-message').textContent = message;
    stopStatusPolling();
}

// Reset form
function resetForm() {
    if (currentFileId) {
        fetch(`/api/file/${currentFileId}`, { method: 'DELETE' }).catch(() => { });
    }
    currentFileId = null;

    document.getElementById('upload-section').classList.remove('hidden');
    optionsSection.classList.add('hidden');
    progressSection.classList.add('hidden');
    resultSection.classList.add('hidden');
    errorSection.classList.add('hidden');

    resetUploadArea();
    sourceLangSelect.value = 'en';
    targetLangSelect.value = 'ko';
    document.getElementById('review-loops').value = '5';
}
