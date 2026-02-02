// State
let uploadedFiles = []; // Array of {fileId, filename, slideCount, textCount, status}
let currentProcessingIndex = -1;
let statusPollInterval = null;
let lastLogMessage = '';
let pollErrorCount = 0;
const MAX_POLL_ERRORS = 5;

// DOM Elements
const uploadArea = document.getElementById('upload-area');
const fileInput = document.getElementById('file-input');
const fileList = document.getElementById('file-list');
const optionsSection = document.getElementById('options-section');
const progressSection = document.getElementById('progress-section');
const resultSection = document.getElementById('result-section');
const errorSection = document.getElementById('error-section');
const sourceLangSelect = document.getElementById('source-lang');
const targetLangSelect = document.getElementById('target-lang');
const targetFontSelect = document.getElementById('target-font');

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    loadLanguages();
    loadFonts();
    setupUploadHandlers();
    loadOptionsFromLocalStorage();
    setupOptionChangeListeners();
});

// Setup change listeners for all options to auto-save
function setupOptionChangeListeners() {
    // Language selects
    sourceLangSelect.addEventListener('change', saveOptionsToLocalStorage);
    targetLangSelect.addEventListener('change', saveOptionsToLocalStorage);

    // Font select
    targetFontSelect.addEventListener('change', saveOptionsToLocalStorage);

    // Visual QA iterations dropdown
    document.getElementById('visual-qa-iterations').addEventListener('change', saveOptionsToLocalStorage);

    // Translation style radio buttons
    document.querySelectorAll('input[name="translation-style"]').forEach(radio => {
        radio.addEventListener('change', saveOptionsToLocalStorage);
    });

    // OCR checkboxes
    const enableOcrCheckbox = document.getElementById('enable-ocr');
    const ocrRetryOption = document.getElementById('ocr-retry-option');
    const enableOcrRetryCheckbox = document.getElementById('enable-ocr-retry');

    enableOcrCheckbox.addEventListener('change', () => {
        // Toggle retry option visibility
        if (enableOcrCheckbox.checked) {
            ocrRetryOption.classList.remove('disabled');
        } else {
            ocrRetryOption.classList.add('disabled');
            enableOcrRetryCheckbox.checked = false;
        }
        saveOptionsToLocalStorage();
    });

    enableOcrRetryCheckbox.addEventListener('change', saveOptionsToLocalStorage);
}

// Save options to localStorage
function saveOptionsToLocalStorage() {
    const options = {
        sourceLang: sourceLangSelect.value,
        targetLang: targetLangSelect.value,
        targetFont: targetFontSelect.value,
        visualQaIterations: document.getElementById('visual-qa-iterations').value,
        translationStyle: document.querySelector('input[name="translation-style"]:checked')?.value || 'technical',
        enableOcr: document.getElementById('enable-ocr').checked,
        enableOcrRetry: document.getElementById('enable-ocr-retry').checked,
    };
    localStorage.setItem('ppt-translator-options', JSON.stringify(options));
}

// Load options from localStorage
function loadOptionsFromLocalStorage() {
    try {
        const saved = localStorage.getItem('ppt-translator-options');
        if (!saved) return;

        const options = JSON.parse(saved);

        // Apply saved options after a short delay (to ensure languages/fonts are loaded)
        setTimeout(() => {
            if (options.sourceLang) {
                sourceLangSelect.value = options.sourceLang;
            }
            if (options.targetLang) {
                targetLangSelect.value = options.targetLang;
            }
            if (options.targetFont) {
                targetFontSelect.value = options.targetFont;
            }
            // Handle both old boolean format and new iteration count format
            if (options.visualQaIterations !== undefined) {
                document.getElementById('visual-qa-iterations').value = options.visualQaIterations;
            } else if (typeof options.enableVisualQA === 'boolean') {
                // Migrate old format: true -> 1, false -> 0
                document.getElementById('visual-qa-iterations').value = options.enableVisualQA ? '1' : '0';
            }
            if (options.translationStyle) {
                const styleRadio = document.querySelector(`input[name="translation-style"][value="${options.translationStyle}"]`);
                if (styleRadio) {
                    styleRadio.checked = true;
                }
            }
            // OCR options
            const enableOcrCheckbox = document.getElementById('enable-ocr');
            const ocrRetryOption = document.getElementById('ocr-retry-option');
            const enableOcrRetryCheckbox = document.getElementById('enable-ocr-retry');

            if (options.enableOcr !== undefined) {
                enableOcrCheckbox.checked = options.enableOcr;
                if (!options.enableOcr) {
                    ocrRetryOption.classList.add('disabled');
                }
            }
            if (options.enableOcrRetry !== undefined) {
                enableOcrRetryCheckbox.checked = options.enableOcrRetry;
            }
        }, 500);
    } catch (error) {
        console.error('Failed to load saved options:', error);
    }
}

// Load available languages (XSS-safe)
async function loadLanguages() {
    try {
        const response = await fetch('/api/languages');
        if (!response.ok) throw new Error('언어 목록을 불러올 수 없습니다');

        const data = await response.json();

        data.languages.forEach(lang => {
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

// Load available fonts
async function loadFonts() {
    try {
        const response = await fetch('/api/fonts');
        if (!response.ok) throw new Error('폰트 목록을 불러올 수 없습니다');

        const data = await response.json();

        targetFontSelect.innerHTML = '';

        data.fonts.forEach(font => {
            const option = document.createElement('option');
            option.value = font.id;
            option.textContent = font.display_name;
            if (font.id === data.default) {
                option.selected = true;
            }
            targetFontSelect.appendChild(option);
        });
    } catch (error) {
        console.error('Failed to load fonts:', error);
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
    uploadArea.addEventListener('click', () => fileInput.click());

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
        const files = Array.from(e.dataTransfer.files);
        handleFilesSelect(files);
    });

    fileInput.addEventListener('change', (e) => {
        const files = Array.from(e.target.files);
        handleFilesSelect(files);
    });
}

// Handle multiple files selection
async function handleFilesSelect(files) {
    const validFiles = files.filter(file => {
        if (!file.name.toLowerCase().endsWith('.pptx')) {
            showInlineError(`${file.name}: .pptx 파일만 지원됩니다.`);
            return false;
        }
        const maxSize = 50 * 1024 * 1024;
        if (file.size > maxSize) {
            showInlineError(`${file.name}: 파일 크기가 너무 큽니다. 최대 50MB까지 지원됩니다.`);
            return false;
        }
        return true;
    });

    if (validFiles.length === 0) return;

    setUploadAreaLoading(true);

    for (const file of validFiles) {
        await uploadSingleFile(file);
    }

    setUploadAreaLoading(false);
    updateFileListUI();

    if (uploadedFiles.length > 0) {
        optionsSection.classList.remove('hidden');
    }
}

// Upload a single file
async function uploadSingleFile(file) {
    const formData = new FormData();
    formData.append('file', file);

    try {
        const response = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });

        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || '파일 업로드에 실패했습니다');
        }

        const data = await response.json();
        uploadedFiles.push({
            fileId: data.file_id,
            filename: data.filename,
            slideCount: data.slide_count,
            textCount: data.text_count,
            status: 'uploaded' // uploaded, processing, completed, error
        });

    } catch (error) {
        showInlineError(`${file.name}: ${error.message}`);
    }
}

// Update file list UI
function updateFileListUI() {
    fileList.innerHTML = '';

    if (uploadedFiles.length === 0) {
        fileList.classList.add('hidden');
        uploadArea.classList.remove('hidden');
        return;
    }

    uploadArea.classList.add('hidden');
    fileList.classList.remove('hidden');

    uploadedFiles.forEach((file, index) => {
        const fileItem = document.createElement('div');
        fileItem.className = `file-item ${file.status}`;
        fileItem.innerHTML = `
            <div class="file-details">
                <span class="file-name">${escapeHtml(file.filename)}</span>
                <span class="file-stats">슬라이드 ${file.slideCount}개, 텍스트 ${file.textCount}개</span>
                <span class="file-status">${getStatusLabel(file.status)}</span>
            </div>
            <div class="file-actions">
                ${file.status === 'completed' ? `<button class="btn-download-small" onclick="downloadSingleFile('${file.fileId}')">다운로드</button>` : ''}
                ${file.status === 'uploaded' ? `<button class="btn-remove" onclick="removeFileByIndex(${index})">✕</button>` : ''}
            </div>
        `;
        fileList.appendChild(fileItem);
    });

    // Add "Add more files" button
    const addMoreBtn = document.createElement('button');
    addMoreBtn.className = 'btn-add-more';
    addMoreBtn.textContent = '+ 파일 추가';
    addMoreBtn.onclick = () => fileInput.click();
    fileList.appendChild(addMoreBtn);
}

// Get status label
function getStatusLabel(status) {
    const labels = {
        'uploaded': '대기중',
        'processing': '번역중...',
        'completed': '완료',
        'error': '오류'
    };
    return labels[status] || status;
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
    } else {
        resetUploadArea();
    }
}

// Remove file by index
function removeFileByIndex(index) {
    const file = uploadedFiles[index];
    if (file) {
        fetch(`/api/file/${file.fileId}`, { method: 'DELETE' }).catch(() => { });
        uploadedFiles.splice(index, 1);
        updateFileListUI();

        if (uploadedFiles.length === 0) {
            optionsSection.classList.add('hidden');
        }
    }
}

// Download a single file
function downloadSingleFile(fileId) {
    window.location.href = `/api/download/${fileId}`;
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
    hint.textContent = '.pptx 파일만 지원 (최대 50MB, 여러 파일 선택 가능)';

    uploadArea.appendChild(icon);
    uploadArea.appendChild(text);
    uploadArea.appendChild(hint);

    fileInput.value = '';
}

// Start translation for all files
async function startTranslation() {
    const sourceLang = sourceLangSelect.value;
    const targetLang = targetLangSelect.value;
    const targetFont = targetFontSelect.value;
    const visualQaIterations = parseInt(document.getElementById('visual-qa-iterations').value) || 0;
    const translationStyle = document.querySelector('input[name="translation-style"]:checked')?.value || 'technical';
    const enableOcr = document.getElementById('enable-ocr').checked;
    const enableOcrRetry = document.getElementById('enable-ocr-retry').checked;

    if (!sourceLang || !targetLang) {
        showInlineError('원본 언어와 목표 언어를 모두 선택해주세요');
        return;
    }

    if (sourceLang === targetLang) {
        showInlineError('원본 언어와 목표 언어가 같습니다');
        return;
    }

    const filesToProcess = uploadedFiles.filter(f => f.status === 'uploaded');
    if (filesToProcess.length === 0) {
        showInlineError('번역할 파일이 없습니다');
        return;
    }

    // Save options to localStorage for next time
    saveOptionsToLocalStorage();

    // Disable button
    const startBtn = document.getElementById('start-btn');
    startBtn.disabled = true;
    startBtn.textContent = '시작 중...';

    // Show progress section
    optionsSection.classList.add('hidden');
    progressSection.classList.remove('hidden');
    clearActivityLog();

    // Process files sequentially
    for (let i = 0; i < uploadedFiles.length; i++) {
        if (uploadedFiles[i].status !== 'uploaded') continue;

        currentProcessingIndex = i;
        uploadedFiles[i].status = 'processing';
        updateFileListUI();

        addActivityLog(`파일 ${i + 1}/${uploadedFiles.length} 번역 시작: ${uploadedFiles[i].filename}`);

        try {
            await processFile(uploadedFiles[i], sourceLang, targetLang, targetFont, visualQaIterations, translationStyle, enableOcr, enableOcrRetry);
            uploadedFiles[i].status = 'completed';
            addActivityLog(`파일 ${i + 1}/${uploadedFiles.length} 완료: ${uploadedFiles[i].filename}`);
        } catch (error) {
            uploadedFiles[i].status = 'error';
            addActivityLog(`파일 ${i + 1}/${uploadedFiles.length} 오류: ${error.message}`);
        }

        updateFileListUI();
    }

    currentProcessingIndex = -1;
    showAllResults();
}

// Process a single file
async function processFile(file, sourceLang, targetLang, targetFont, visualQaIterations, translationStyle, enableOcr, enableOcrRetry) {
    const formData = new FormData();
    formData.append('source_language', sourceLang);
    formData.append('target_language', targetLang);
    formData.append('target_font', targetFont);
    formData.append('visual_qa_iterations', visualQaIterations.toString());
    formData.append('translation_style', translationStyle);
    formData.append('enable_ocr', enableOcr ? 'true' : 'false');
    formData.append('enable_ocr_retry', enableOcrRetry ? 'true' : 'false');

    const response = await fetch(`/api/translate/${file.fileId}`, {
        method: 'POST',
        body: formData
    });

    if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || '번역 시작에 실패했습니다');
    }

    // Poll for completion
    return new Promise((resolve, reject) => {
        pollErrorCount = 0;
        pollFileStatus(file.fileId, resolve, reject);
    });
}

// Poll file status
function pollFileStatus(fileId, resolve, reject) {
    let pollDelay = 1000;
    const maxDelay = 5000;

    async function poll() {
        try {
            const response = await fetch(`/api/status/${fileId}`);
            if (!response.ok) throw new Error('상태를 가져올 수 없습니다');

            const status = await response.json();
            updateProgress(status);
            pollErrorCount = 0;
            pollDelay = 1000;

            if (status.status === 'completed') {
                resolve(status);
            } else if (status.status === 'error') {
                reject(new Error(status.message));
            } else {
                setTimeout(poll, pollDelay);
            }
        } catch (error) {
            pollErrorCount++;
            if (pollErrorCount >= MAX_POLL_ERRORS) {
                reject(new Error('서버 연결이 끊어졌습니다'));
            } else {
                pollDelay = Math.min(pollDelay * 1.5, maxDelay);
                addActivityLog(`연결 재시도 중... (${pollErrorCount}/${MAX_POLL_ERRORS})`);
                setTimeout(poll, pollDelay);
            }
        }
    }

    poll();
}

// Cancel translation
function cancelTranslation() {
    stopStatusPolling();

    // Reset all processing files
    uploadedFiles.forEach(file => {
        if (file.status === 'processing') {
            file.status = 'uploaded';
        }
    });
    updateFileListUI();

    progressSection.classList.add('hidden');
    optionsSection.classList.remove('hidden');

    const startBtn = document.getElementById('start-btn');
    startBtn.disabled = false;
    startBtn.textContent = '번역 시작';
}

function stopStatusPolling() {
    if (statusPollInterval) {
        clearTimeout(statusPollInterval);
        statusPollInterval = null;
    }
}

// Update progress UI
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

    if (status.message && status.message !== lastLogMessage) {
        addActivityLog(status.message);
        lastLogMessage = status.message;
    }
}

// Activity log
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

    while (log.children.length > MAX_LOG_ENTRIES) {
        log.removeChild(log.firstChild);
    }

    log.scrollTop = log.scrollHeight;
}

function clearActivityLog() {
    document.getElementById('activity-log').innerHTML = '';
    lastLogMessage = '';
}

// Show all results
async function showAllResults() {
    progressSection.classList.add('hidden');
    resultSection.classList.remove('hidden');

    const completedFiles = uploadedFiles.filter(f => f.status === 'completed');
    const errorFiles = uploadedFiles.filter(f => f.status === 'error');

    const stats = document.getElementById('result-stats');
    stats.innerHTML = '';

    const summary = document.createElement('p');
    summary.textContent = `완료: ${completedFiles.length}개, 오류: ${errorFiles.length}개`;
    stats.appendChild(summary);

    // Show file list with download buttons in result section
    const resultFiles = document.getElementById('result-files');
    resultFiles.innerHTML = '';

    // Completed files
    completedFiles.forEach(file => {
        const fileDiv = document.createElement('div');
        fileDiv.className = 'result-file-item completed';
        fileDiv.innerHTML = `
            <div class="result-file-info">
                <span class="result-file-name">${escapeHtml(file.filename)}</span>
                <span class="result-file-stats">슬라이드 ${file.slideCount}개</span>
            </div>
            <button class="btn-primary btn-download-file" onclick="downloadSingleFile('${file.fileId}')">
                📥 다운로드
            </button>
        `;
        resultFiles.appendChild(fileDiv);
    });

    // Error files
    errorFiles.forEach(file => {
        const fileDiv = document.createElement('div');
        fileDiv.className = 'result-file-item error';
        fileDiv.innerHTML = `
            <div class="result-file-info">
                <span class="result-file-name">${escapeHtml(file.filename)}</span>
                <span class="result-file-error">번역 실패</span>
            </div>
        `;
        resultFiles.appendChild(fileDiv);
    });

    // Download all button if multiple files completed
    if (completedFiles.length > 1) {
        const downloadAllDiv = document.createElement('div');
        downloadAllDiv.className = 'download-all-container';
        downloadAllDiv.innerHTML = `
            <button class="btn-primary btn-download-all" onclick="downloadAllFiles()">
                📦 모든 파일 다운로드
            </button>
        `;
        resultFiles.appendChild(downloadAllDiv);
    }

    // Load QA history for the last completed file
    if (completedFiles.length > 0) {
        const lastCompleted = completedFiles[completedFiles.length - 1];
        await loadQAHistory(lastCompleted.fileId);
    }
}

// Download all completed files
function downloadAllFiles() {
    const completedFiles = uploadedFiles.filter(f => f.status === 'completed');
    completedFiles.forEach((file, index) => {
        // Delay each download slightly to avoid browser blocking
        setTimeout(() => {
            const link = document.createElement('a');
            link.href = `/api/download/${file.fileId}`;
            link.download = '';
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }, index * 500);
    });
}

// Load QA history
async function loadQAHistory(fileId) {
    const qaSection = document.getElementById('qa-section');
    const qaIterations = document.getElementById('qa-iterations');

    try {
        const response = await fetch(`/api/qa-history/${fileId}`);
        if (!response.ok) {
            qaSection.classList.add('hidden');
            return;
        }

        const data = await response.json();

        if (!data.iterations || data.iterations.length === 0) {
            qaSection.classList.add('hidden');
            return;
        }

        qaIterations.innerHTML = '';

        data.iterations.forEach(iteration => {
            const iterDiv = document.createElement('div');
            iterDiv.className = 'qa-iteration';

            const isSkipped = iteration.overall_score === -1;

            const header = document.createElement('div');
            header.className = 'qa-iteration-header';

            if (isSkipped) {
                header.innerHTML = `
                    <h3>검증 ${iteration.iteration}회차</h3>
                    <span class="qa-score skipped">시각적 QA 불가</span>
                `;
            } else {
                header.innerHTML = `
                    <h3>검증 ${iteration.iteration}회차</h3>
                    <span class="qa-score ${iteration.overall_score >= 85 ? 'good' : 'needs-work'}">
                        점수: ${iteration.overall_score}/100
                    </span>
                `;
            }
            iterDiv.appendChild(header);

            if (iteration.critical_issues_count > 0 || iteration.texts_retranslated > 0) {
                const summary = document.createElement('div');
                summary.className = 'qa-summary';
                summary.textContent = `발견된 문제: ${iteration.critical_issues_count}개, 재번역: ${iteration.texts_retranslated}개`;
                iterDiv.appendChild(summary);
            }

            if (iteration.algorithm_improvements && iteration.algorithm_improvements.length > 0) {
                const improvements = document.createElement('div');
                improvements.className = 'qa-improvements';
                improvements.innerHTML = '<strong>개선 제안:</strong>';
                const list = document.createElement('ul');
                iteration.algorithm_improvements.forEach(imp => {
                    const li = document.createElement('li');
                    li.textContent = imp;
                    list.appendChild(li);
                });
                improvements.appendChild(list);
                iterDiv.appendChild(improvements);
            }

            if (iteration.slide_comparisons && iteration.slide_comparisons.length > 0) {
                const slidesDiv = document.createElement('div');
                slidesDiv.className = 'qa-slides';

                iteration.slide_comparisons.forEach(slide => {
                    const slideDiv = document.createElement('div');
                    slideDiv.className = 'qa-slide-comparison';

                    const slideHeader = document.createElement('div');
                    slideHeader.className = 'slide-header';
                    slideHeader.innerHTML = `
                        <span>슬라이드 ${slide.slide_number}</span>
                        <span class="slide-score ${slide.quality_score >= 85 ? 'good' : 'needs-work'}">
                            ${slide.quality_score}/100
                        </span>
                    `;
                    slideDiv.appendChild(slideHeader);

                    const imagesDiv = document.createElement('div');
                    imagesDiv.className = 'slide-images';

                    const origContainer = document.createElement('div');
                    origContainer.className = 'image-container';
                    origContainer.innerHTML = `
                        <span class="image-label">원본</span>
                        <img src="${slide.original_image_url}" alt="원본 슬라이드 ${slide.slide_number}" loading="lazy">
                    `;

                    const transContainer = document.createElement('div');
                    transContainer.className = 'image-container';
                    transContainer.innerHTML = `
                        <span class="image-label">번역</span>
                        <img src="${slide.translated_image_url}" alt="번역된 슬라이드 ${slide.slide_number}" loading="lazy">
                    `;

                    imagesDiv.appendChild(origContainer);
                    imagesDiv.appendChild(transContainer);
                    slideDiv.appendChild(imagesDiv);

                    if (slide.issues && slide.issues.length > 0) {
                        const issuesDiv = document.createElement('div');
                        issuesDiv.className = 'slide-issues';
                        slide.issues.forEach(issue => {
                            const issueDiv = document.createElement('div');
                            issueDiv.className = `issue ${issue.severity}`;
                            issueDiv.innerHTML = `
                                <span class="issue-type">${getIssueTypeLabel(issue.issue_type)}</span>
                                <span class="issue-text">${escapeHtml(issue.original_text)}</span>
                                <span class="issue-desc">${escapeHtml(issue.description)}</span>
                            `;
                            issuesDiv.appendChild(issueDiv);
                        });
                        slideDiv.appendChild(issuesDiv);
                    }

                    slidesDiv.appendChild(slideDiv);
                });

                iterDiv.appendChild(slidesDiv);
            }

            qaIterations.appendChild(iterDiv);
        });

        qaSection.classList.remove('hidden');
        displayAlgorithmSuggestions(data);

    } catch (error) {
        console.error('Failed to load QA history:', error);
        qaSection.classList.add('hidden');
    }
}

// Display algorithm suggestions
function displayAlgorithmSuggestions(qaData) {
    const suggestionsSection = document.getElementById('suggestions-section');
    const suggestionsContent = document.getElementById('suggestions-content');

    if (!qaData.iterations || qaData.iterations.length === 0) {
        suggestionsSection.classList.add('hidden');
        return;
    }

    const allSuggestions = new Set();
    const allIssues = [];

    qaData.iterations.forEach(iteration => {
        if (iteration.algorithm_improvements) {
            iteration.algorithm_improvements.forEach(imp => allSuggestions.add(imp));
        }

        if (iteration.slide_comparisons) {
            iteration.slide_comparisons.forEach(slide => {
                if (slide.issues) {
                    slide.issues.forEach(issue => {
                        allIssues.push({
                            slide: slide.slide_number,
                            type: issue.issue_type,
                            text: issue.original_text,
                            description: issue.description,
                            suggestion: issue.suggestion
                        });
                    });
                }
            });
        }
    });

    let suggestionsText = '=== 알고리즘 개선 제안 ===\n\n';
    suggestionsText += `최종 점수: ${qaData.final_score}/100\n\n`;

    suggestionsText += '--- 개선 제안 ---\n';
    allSuggestions.forEach(suggestion => {
        suggestionsText += `• ${suggestion}\n`;
    });

    suggestionsText += '\n--- 발견된 문제점 ---\n';
    allIssues.forEach(issue => {
        suggestionsText += `[슬라이드 ${issue.slide}] ${getIssueTypeLabel(issue.type)}\n`;
        if (issue.text) suggestionsText += `  원본: ${issue.text}\n`;
        if (issue.description) suggestionsText += `  설명: ${issue.description}\n`;
        if (issue.suggestion) suggestionsText += `  제안: ${issue.suggestion}\n`;
        suggestionsText += '\n';
    });

    suggestionsContent.innerHTML = '';
    const pre = document.createElement('pre');
    pre.id = 'suggestions-text';
    pre.textContent = suggestionsText;
    suggestionsContent.appendChild(pre);

    suggestionsSection.classList.remove('hidden');
}

// Copy suggestions
async function copySuggestions() {
    const suggestionsText = document.getElementById('suggestions-text');
    if (!suggestionsText) return;

    try {
        await navigator.clipboard.writeText(suggestionsText.textContent);
        const copyBtn = document.querySelector('.btn-copy');
        const originalText = copyBtn.innerHTML;
        copyBtn.innerHTML = '<span aria-hidden="true">✅</span> 복사됨!';
        setTimeout(() => {
            copyBtn.innerHTML = originalText;
        }, 2000);
    } catch (err) {
        console.error('Failed to copy:', err);
        const range = document.createRange();
        range.selectNode(suggestionsText);
        window.getSelection().removeAllRanges();
        window.getSelection().addRange(range);
    }
}

// Helper functions
function getIssueTypeLabel(type) {
    const labels = {
        'untranslated': '미번역',
        'overflow': '텍스트 넘침',
        'layout': '레이아웃 문제',
        'missing': '텍스트 누락',
    };
    return labels[type] || type;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Download all completed files
function downloadFile() {
    const completedFiles = uploadedFiles.filter(f => f.status === 'completed');
    completedFiles.forEach(file => {
        window.open(`/api/download/${file.fileId}`, '_blank');
    });
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
    // Delete all uploaded files
    uploadedFiles.forEach(file => {
        fetch(`/api/file/${file.fileId}`, { method: 'DELETE' }).catch(() => { });
    });
    uploadedFiles = [];

    document.getElementById('upload-section').classList.remove('hidden');
    optionsSection.classList.add('hidden');
    progressSection.classList.add('hidden');
    resultSection.classList.add('hidden');
    errorSection.classList.add('hidden');

    const qaSection = document.getElementById('qa-section');
    if (qaSection) {
        qaSection.classList.add('hidden');
        document.getElementById('qa-iterations').innerHTML = '';
    }

    const suggestionsSection = document.getElementById('suggestions-section');
    if (suggestionsSection) {
        suggestionsSection.classList.add('hidden');
        document.getElementById('suggestions-content').innerHTML = '';
    }

    fileList.classList.add('hidden');
    fileList.innerHTML = '';
    resetUploadArea();
    sourceLangSelect.value = 'en';
    targetLangSelect.value = 'ko';

    const startBtn = document.getElementById('start-btn');
    if (startBtn) {
        startBtn.disabled = false;
        startBtn.textContent = '번역 시작';
    }
}

// Cleanup on page unload
window.addEventListener('beforeunload', () => {
    uploadedFiles.forEach(file => {
        navigator.sendBeacon(`/api/file/${file.fileId}`, new FormData());
    });
});
