// State
let currentFileId = null;
let statusPollInterval = null;
let lastLogMessage = '';
let pollErrorCount = 0;
const MAX_POLL_ERRORS = 5;

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

// Load available languages (XSS-safe)
async function loadLanguages() {
    try {
        const response = await fetch('/api/languages');
        if (!response.ok) throw new Error('언어 목록을 불러올 수 없습니다');

        const data = await response.json();

        data.languages.forEach(lang => {
            // Use DOM API instead of innerHTML to prevent XSS
            const sourceOption = document.createElement('option');
            sourceOption.value = lang.code;
            sourceOption.textContent = lang.name;
            sourceLangSelect.appendChild(sourceOption);

            const targetOption = document.createElement('option');
            targetOption.value = lang.code;
            targetOption.textContent = lang.name;
            targetLangSelect.appendChild(targetOption);
        });

        // Set defaults
        sourceLangSelect.value = 'en';
        targetLangSelect.value = 'ko';
    } catch (error) {
        console.error('Failed to load languages:', error);
        showInlineError('언어 목록을 불러오는데 실패했습니다. 페이지를 새로고침해주세요.');
    }
}

// Show inline error (non-destructive)
function showInlineError(message) {
    const existingError = document.querySelector('.inline-error');
    if (existingError) existingError.remove();

    const errorDiv = document.createElement('div');
    errorDiv.className = 'inline-error';
    errorDiv.textContent = message;
    errorDiv.setAttribute('role', 'alert');

    const container = document.querySelector('.container main');
    container.insertBefore(errorDiv, container.firstChild);

    setTimeout(() => errorDiv.remove(), 5000);
}

// Setup upload handlers
function setupUploadHandlers() {
    // Click handler
    uploadArea.addEventListener('click', () => fileInput.click());

    // Keyboard handler for accessibility
    uploadArea.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            fileInput.click();
        }
    });

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
    // Only accept .pptx (python-pptx doesn't support .ppt)
    if (!file.name.toLowerCase().endsWith('.pptx')) {
        showInlineError('.pptx 파일만 지원됩니다. .ppt 파일은 .pptx로 변환 후 업로드하세요.');
        return;
    }

    // Check file size (50MB limit)
    const maxSize = 50 * 1024 * 1024;
    if (file.size > maxSize) {
        showInlineError('파일 크기가 너무 큽니다. 최대 50MB까지 지원됩니다.');
        return;
    }

    const formData = new FormData();
    formData.append('file', file);

    try {
        // Update upload area to show loading state
        setUploadAreaLoading(true);

        const response = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || '파일 업로드에 실패했습니다');
        }

        const data = await response.json();
        currentFileId = data.file_id;

        // Show file info (XSS-safe)
        uploadArea.classList.add('hidden');
        fileInfo.classList.remove('hidden');
        fileInfo.querySelector('.file-name').textContent = data.filename;
        fileInfo.querySelector('.file-stats').textContent =
            `슬라이드 ${data.slide_count}개, 텍스트 ${data.text_count}개`;

        // Show options
        optionsSection.classList.remove('hidden');

    } catch (error) {
        resetUploadArea();
        showInlineError(error.message);
    }
}

// Set upload area loading state
function setUploadAreaLoading(isLoading) {
    if (isLoading) {
        uploadArea.innerHTML = '';
        const icon = document.createElement('div');
        icon.className = 'upload-icon';
        icon.textContent = '⏳';
        const text = document.createElement('p');
        text.textContent = '업로드 중...';
        uploadArea.appendChild(icon);
        uploadArea.appendChild(text);
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

// Reset upload area (XSS-safe)
function resetUploadArea() {
    uploadArea.classList.remove('hidden');
    uploadArea.innerHTML = '';

    const icon = document.createElement('span');
    icon.className = 'upload-icon';
    icon.setAttribute('aria-hidden', 'true');
    icon.textContent = '📄';

    const text = document.createElement('p');
    text.textContent = '파일을 드래그하거나 클릭하여 선택';

    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = '.pptx 파일만 지원 (최대 50MB)';

    uploadArea.appendChild(icon);
    uploadArea.appendChild(text);
    uploadArea.appendChild(hint);

    fileInfo.classList.add('hidden');
    fileInput.value = '';
}

// Start translation
async function startTranslation() {
    const sourceLang = sourceLangSelect.value;
    const targetLang = targetLangSelect.value;

    if (!sourceLang || !targetLang) {
        showInlineError('원본 언어와 목표 언어를 모두 선택해주세요');
        return;
    }

    if (sourceLang === targetLang) {
        showInlineError('원본 언어와 목표 언어가 같습니다');
        return;
    }

    // Disable button and show loading
    const startBtn = document.getElementById('start-btn');
    startBtn.disabled = true;
    startBtn.textContent = '시작 중...';

    const formData = new FormData();
    formData.append('source_language', sourceLang);
    formData.append('target_language', targetLang);

    try {
        const response = await fetch(`/api/translate/${currentFileId}`, {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || '번역 시작에 실패했습니다');
        }

        // Show progress section
        optionsSection.classList.add('hidden');
        progressSection.classList.remove('hidden');
        clearActivityLog();
        addActivityLog('번역이 시작되었습니다');

        // Reset poll error count
        pollErrorCount = 0;

        // Start polling for status
        startStatusPolling();

    } catch (error) {
        startBtn.disabled = false;
        startBtn.textContent = '번역 시작';
        showInlineError(error.message);
    }
}

// Cancel translation
function cancelTranslation() {
    stopStatusPolling();
    if (currentFileId) {
        fetch(`/api/file/${currentFileId}`, { method: 'DELETE' }).catch(() => { });
    }

    // Go back to options
    progressSection.classList.add('hidden');
    optionsSection.classList.remove('hidden');

    // Reset start button
    const startBtn = document.getElementById('start-btn');
    startBtn.disabled = false;
    startBtn.textContent = '번역 시작';
}

// Status polling with exponential backoff
function startStatusPolling() {
    let pollDelay = 1000;
    const maxDelay = 5000;

    async function poll() {
        if (!statusPollInterval) return;

        try {
            const response = await fetch(`/api/status/${currentFileId}`);
            if (!response.ok) {
                throw new Error('상태를 가져올 수 없습니다');
            }

            const status = await response.json();
            updateProgress(status);
            pollErrorCount = 0;
            pollDelay = 1000; // Reset delay on success

            if (status.status === 'completed') {
                stopStatusPolling();
                showResult(status);
            } else if (status.status === 'error') {
                stopStatusPolling();
                showError(status.message);
            } else {
                statusPollInterval = setTimeout(poll, pollDelay);
            }
        } catch (error) {
            console.error('Status poll error:', error);
            pollErrorCount++;

            if (pollErrorCount >= MAX_POLL_ERRORS) {
                stopStatusPolling();
                showError('서버 연결이 끊어졌습니다. 다시 시도해주세요.');
            } else {
                pollDelay = Math.min(pollDelay * 1.5, maxDelay);
                addActivityLog(`연결 재시도 중... (${pollErrorCount}/${MAX_POLL_ERRORS})`);
                statusPollInterval = setTimeout(poll, pollDelay);
            }
        }
    }

    statusPollInterval = setTimeout(poll, pollDelay);
}

function stopStatusPolling() {
    if (statusPollInterval) {
        clearTimeout(statusPollInterval);
        statusPollInterval = null;
    }
}

// Update progress UI (XSS-safe)
function updateProgress(status) {
    const progressFill = document.getElementById('progress-fill');
    const progressText = document.getElementById('progress-text');
    const statusMessage = document.getElementById('status-message');
    const currentItem = document.getElementById('current-item');
    const progressBar = document.querySelector('.progress-bar');

    const progress = status.progress || 0;

    progressFill.style.width = `${progress}%`;
    progressText.textContent = `${progress}%`;
    progressBar.setAttribute('aria-valuenow', progress);

    statusMessage.textContent = status.message || '처리 중...';

    const totalSlides = status.total_slides || 0;
    const currentSlide = status.current_slide || 0;
    currentItem.textContent = totalSlides > 0 ? `${currentSlide} / ${totalSlides}` : '-';

    // Add to activity log
    if (status.message && status.message !== lastLogMessage) {
        addActivityLog(status.message);
        lastLogMessage = status.message;
    }
}

// Activity log (XSS-safe, with limit)
const MAX_LOG_ENTRIES = 50;

function addActivityLog(message) {
    const log = document.getElementById('activity-log');
    const time = new Date().toLocaleTimeString('ko-KR');

    const entry = document.createElement('div');
    entry.className = 'log-entry';

    const timeSpan = document.createElement('span');
    timeSpan.className = 'log-time';
    timeSpan.textContent = `[${time}]`;

    entry.appendChild(timeSpan);
    entry.appendChild(document.createTextNode(' ' + message));

    log.appendChild(entry);

    // Limit log entries
    while (log.children.length > MAX_LOG_ENTRIES) {
        log.removeChild(log.firstChild);
    }

    log.scrollTop = log.scrollHeight;
}

function clearActivityLog() {
    document.getElementById('activity-log').innerHTML = '';
    lastLogMessage = '';
}

// Show result (XSS-safe)
function showResult(status) {
    progressSection.classList.add('hidden');
    resultSection.classList.remove('hidden');

    const stats = document.getElementById('result-stats');
    stats.innerHTML = ''; // Clear existing

    const slideInfo = document.createElement('p');
    slideInfo.textContent = `총 슬라이드: ${status.total_slides || 0}개`;

    stats.appendChild(slideInfo);
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

    // Reset start button
    const startBtn = document.getElementById('start-btn');
    if (startBtn) {
        startBtn.disabled = false;
        startBtn.textContent = '번역 시작';
    }
}

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    if (currentFileId) {
        // Use sendBeacon for reliable cleanup
        navigator.sendBeacon(`/api/file/${currentFileId}`, new FormData());
    }
});
