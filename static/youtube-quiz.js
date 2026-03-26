(function () {
    const homeView = document.getElementById('home-view');
    const quizView = document.getElementById('quiz-view');
    const youtubeUrlInput = document.getElementById('youtubeUrlInput');
    const generateBtn = document.getElementById('generateBtn');
    const viewPastSessionsBtn = document.getElementById('viewPastSessionsBtn');
    const generationStatus = document.getElementById('generationStatus');
    const mainSessionsModal = document.getElementById('main-sessions-modal');
    const sessionsList = document.getElementById('sessions-list');

    const quizTitleEl = document.getElementById('quiz-title');
    const questionCounterElement = document.getElementById('question-counter');
    const questionTextElement = document.getElementById('question-text');
    const revealChoicesButton = document.getElementById('reveal-choices-btn');
    const answerOptionsElement = document.getElementById('answer-options');
    const explanationElement = document.getElementById('explanation');
    const prevButton = document.getElementById('prev-button');
    const nextButton = document.getElementById('next-button');
    const completionMessageElement = document.getElementById('completion-message');
    const quizContentElement = document.querySelector('.quiz-content');
    const navigationButtonsElement = document.querySelector('.navigation-buttons');
    const profileBtnEl = document.getElementById('profile-btn');
    const profilePanelEl = document.getElementById('profile-panel');
    const settingsPanelEl = document.getElementById('settings-panel');
    const revealChoicesToggleEl = document.getElementById('reveal-choices-toggle');
    const pastSessionsPanelEl = document.getElementById('past-sessions-panel');
    const pastSessionsListEl = document.getElementById('past-sessions-list');
    const favoriteToggle = document.getElementById('favorite-toggle');
    const resetQuizBtn = document.getElementById('reset-quiz-btn');
    const navigatorOverlay = document.getElementById('navigator-overlay');
    const navigatorGrid = document.getElementById('navigator-grid');
    const navigatorSourceLink = document.getElementById('navigator-source-link');
    const navigatorClose = document.getElementById('navigator-close');
    const viewProgressReviewBtn = document.getElementById('view-progress-review-btn');
    const reviewListEl = document.getElementById('review-list');
    const filterBtns = document.querySelectorAll('.filter-btn[data-filter]');
    const copyReviewBtn = document.getElementById('copy-review-btn');
    const backToQuizBtn = document.getElementById('back-to-quiz-btn');

    let activeSessionData = null;
    let quizData = [];
    let currentQuestionIndex = 0;
    let userAnswers = [];
    let currentFilter = 'all';
    let favoriteQuestions = new Set();
    let isPreviewMode = false;
    let generationTimerId = null;
    let generationStartedAt = 0;
    let generationTimerMessage = '';
    const quizSettingsKey = 'quizGeneratorSettings';
    let userSettings = {
        hideChoicesUntilReveal: false
    };

    function getQuizProgressKey() {
        return activeSessionData ? `quizProgress:${activeSessionData.title}` : '';
    }

    function formatDatePrefix(date) {
        return `${date.getFullYear()}.${String(date.getMonth() + 1).padStart(2, '0')}.${String(date.getDate()).padStart(2, '0')}`;
    }

    function formatElapsedTime(totalMilliseconds) {
        const totalSeconds = Math.max(0, Math.floor(totalMilliseconds / 1000));
        const minutes = String(Math.floor(totalSeconds / 60)).padStart(2, '0');
        const seconds = String(totalSeconds % 60).padStart(2, '0');
        return `${minutes}:${seconds}`;
    }

    function clearGenerationTimer() {
        if (generationTimerId) {
            clearInterval(generationTimerId);
            generationTimerId = null;
        }
    }

    function renderGeneratingStatus() {
        const elapsed = formatElapsedTime(Date.now() - generationStartedAt);
        generationStatus.innerHTML = `${generationTimerMessage}<span class="status-timer">${elapsed}</span><span class="loading-dot"></span><span class="loading-dot"></span><span class="loading-dot"></span>`;
        generationStatus.className = 'status-line';
    }

    function showStatus(message, type = '') {
        clearGenerationTimer();
        generationStatus.textContent = message;
        generationStatus.className = 'status-line';
        if (type) generationStatus.classList.add(type);
    }

    function setGeneratingState(isGenerating, message = '') {
        generateBtn.disabled = isGenerating;
        youtubeUrlInput.disabled = isGenerating;
        if (isGenerating) {
            clearGenerationTimer();
            generationTimerMessage = message;
            generationStartedAt = Date.now();
            renderGeneratingStatus();
            generationTimerId = setInterval(renderGeneratingStatus, 1000);
        } else if (message) {
            showStatus(message);
        } else {
            clearGenerationTimer();
            generationStatus.textContent = '';
            generationStatus.className = 'status-line';
        }
    }

    function showHomeView() {
        closeAllOverlays();
        quizView.classList.add('hidden');
        homeView.classList.remove('hidden');
        sessionStorage.removeItem('loadQuizTitle');
        document.title = 'YouTube Quiz Generator';
    }

    function showQuizView() {
        closeAllOverlays();
        homeView.classList.add('hidden');
        quizView.classList.remove('hidden');
    }

    function closeAllOverlays() {
        navigatorOverlay.style.display = 'none';
        mainSessionsModal.style.display = 'none';
        hidePanel(profilePanelEl);
        hidePanel(settingsPanelEl);
        hidePanel(pastSessionsPanelEl);
    }

    function sanitizeDisplayTitle(title) {
        return title.replace(/^\d{4}\.\d{2}\.\d{2}\s+/, '');
    }

    function loadUserSettings() {
        const rawSettings = localStorage.getItem(quizSettingsKey);
        if (!rawSettings) return;
        try {
            const parsedSettings = JSON.parse(rawSettings);
            if (typeof parsedSettings.hideChoicesUntilReveal === 'boolean') {
                userSettings.hideChoicesUntilReveal = parsedSettings.hideChoicesUntilReveal;
            }
        } catch (error) {
            console.warn('Unable to load user settings:', error);
        }
    }

    function saveUserSettings() {
        localStorage.setItem(quizSettingsKey, JSON.stringify(userSettings));
    }

    function saveQuizProgress() {
        if (!activeSessionData) return;
        const payload = {
            currentQuestionIndex,
            userAnswers,
            favoriteQuestions: Array.from(favoriteQuestions)
        };
        localStorage.setItem(getQuizProgressKey(), JSON.stringify(payload));
    }

    function loadQuizProgress() {
        if (!activeSessionData) return;
        const savedProgress = localStorage.getItem(getQuizProgressKey());
        if (!savedProgress) return;
        try {
            const parsed = JSON.parse(savedProgress);
            if (Array.isArray(parsed.userAnswers) && parsed.userAnswers.length === quizData.length) {
                userAnswers = parsed.userAnswers;
            }
            if (Array.isArray(parsed.favoriteQuestions)) {
                favoriteQuestions = new Set(parsed.favoriteQuestions);
            }
            if (Number.isInteger(parsed.currentQuestionIndex)) {
                currentQuestionIndex = Math.max(0, Math.min(parsed.currentQuestionIndex, quizData.length));
            }
        } catch (error) {
            console.warn('Unable to load quiz progress:', error);
        }
    }

    function updateFavoriteButton() {
        const isFavorite = favoriteQuestions.has(currentQuestionIndex);
        favoriteToggle.classList.toggle('active', isFavorite);
        favoriteToggle.textContent = isFavorite ? '★' : '☆';
        favoriteToggle.setAttribute('aria-pressed', isFavorite);
        favoriteToggle.title = isFavorite ? 'Remove from favorites' : 'Save question to favorites';
        favoriteToggle.style.display = currentQuestionIndex >= quizData.length ? 'none' : 'grid';
    }

    function toggleFavorite() {
        if (currentQuestionIndex >= quizData.length) return;
        if (favoriteQuestions.has(currentQuestionIndex)) favoriteQuestions.delete(currentQuestionIndex);
        else favoriteQuestions.add(currentQuestionIndex);
        updateFavoriteButton();
        if (navigatorOverlay.style.display === 'block') buildNavigatorGrid();
        saveQuizProgress();
    }

    function updateNavButtonStates() {
        prevButton.disabled = currentQuestionIndex === 0;
        nextButton.textContent = currentQuestionIndex === quizData.length - 1 ? 'Finish' : 'Next';
    }

    function loadQuestion() {
        if (!activeSessionData) return;
        if (currentQuestionIndex >= quizData.length) {
            showCompletion();
            return;
        }

        quizContentElement.style.display = 'block';
        navigationButtonsElement.style.display = 'flex';
        completionMessageElement.style.display = 'none';
        navigatorOverlay.style.display = 'none';

        explanationElement.style.display = 'none';
        explanationElement.innerHTML = '';
        answerOptionsElement.innerHTML = '';

        const currentQuestion = quizData[currentQuestionIndex];
        const questionHasAnswer = userAnswers[currentQuestionIndex] !== null;

        questionCounterElement.innerHTML = `Question ${currentQuestionIndex + 1} of ${quizData.length} <small>▼</small>`;
        questionTextElement.textContent = currentQuestion.question;

        const shouldHideChoices = userSettings.hideChoicesUntilReveal && !questionHasAnswer;
        revealChoicesButton.style.display = shouldHideChoices ? 'inline-block' : 'none';
        revealChoicesButton.disabled = false;
        answerOptionsElement.style.display = shouldHideChoices ? 'none' : 'flex';

        currentQuestion.options.forEach((option) => {
            const button = document.createElement('button');
            button.textContent = option;
            button.classList.add('option-button');

            if (questionHasAnswer) {
                button.disabled = true;
                const savedAnswer = userAnswers[currentQuestionIndex];
                if (option === savedAnswer.selected) {
                    button.classList.add(savedAnswer.isCorrect ? 'correct' : 'incorrect');
                }
                if (option === currentQuestion.correctAnswer && !savedAnswer.isCorrect) {
                    button.classList.add('reveal-correct');
                }
            } else {
                button.addEventListener('click', selectAnswer);
            }

            answerOptionsElement.appendChild(button);
        });

        if (questionHasAnswer) {
            explanationElement.innerHTML = `<strong>Explanation:</strong> ${currentQuestion.explanation || 'No explanation provided.'}`;
            explanationElement.style.display = 'block';
        }

        updateFavoriteButton();
        updateNavButtonStates();
    }

    function selectAnswer(event) {
        if (userAnswers[currentQuestionIndex] !== null) return;

        const selectedAnswer = event.target.textContent;
        const currentQuestion = quizData[currentQuestionIndex];
        const isCorrect = selectedAnswer === currentQuestion.correctAnswer;

        userAnswers[currentQuestionIndex] = {
            selected: selectedAnswer,
            isCorrect
        };

        answerOptionsElement.querySelectorAll('.option-button').forEach((button) => {
            button.disabled = true;
            if (button.textContent === selectedAnswer) {
                button.classList.add(isCorrect ? 'correct' : 'incorrect');
            }
            if (!isCorrect && button.textContent === currentQuestion.correctAnswer) {
                button.classList.add('reveal-correct');
            }
        });

        explanationElement.innerHTML = `<strong>Explanation:</strong> ${currentQuestion.explanation || 'No explanation provided.'}`;
        explanationElement.style.display = 'block';
        saveQuizProgress();
    }

    function goToNextQuestion() {
        if (currentQuestionIndex < quizData.length - 1) {
            currentQuestionIndex += 1;
            loadQuestion();
        } else if (currentQuestionIndex === quizData.length - 1) {
            currentQuestionIndex += 1;
            showCompletion();
        }
        saveQuizProgress();
    }

    function goToPreviousQuestion() {
        if (currentQuestionIndex > 0) {
            currentQuestionIndex -= 1;
            loadQuestion();
            saveQuizProgress();
        }
    }

    function toggleNavigator() {
        if (navigatorOverlay.style.display === 'block') {
            navigatorOverlay.style.display = 'none';
        } else {
            buildNavigatorGrid();
            navigatorOverlay.style.display = 'block';
        }
    }

    function buildNavigatorGrid() {
        navigatorGrid.innerHTML = '';

        const sourceUrl = activeSessionData && activeSessionData.sourceUrl;
        if (sourceUrl) {
            navigatorSourceLink.href = sourceUrl;
            navigatorSourceLink.style.display = 'block';
        } else {
            navigatorSourceLink.removeAttribute('href');
            navigatorSourceLink.style.display = 'none';
        }

        for (let i = 0; i < quizData.length; i += 1) {
            const item = document.createElement('div');
            item.classList.add('nav-item');
            item.textContent = i + 1;

            if (favoriteQuestions.has(i)) {
                const star = document.createElement('span');
                star.classList.add('nav-favorite-star');
                star.textContent = '★';
                star.setAttribute('aria-hidden', 'true');
                item.appendChild(star);
            }

            if (i === currentQuestionIndex) item.classList.add('current');
            if (userAnswers[i] !== null) {
                item.classList.add(userAnswers[i].isCorrect ? 'correct' : 'incorrect');
            }

            item.addEventListener('click', () => {
                currentQuestionIndex = i;
                loadQuestion();
                saveQuizProgress();
            });
            navigatorGrid.appendChild(item);
        }
    }

    function showCompletion(isPreview = false) {
        isPreviewMode = isPreview;
        quizContentElement.style.display = 'none';
        navigationButtonsElement.style.display = 'none';
        completionMessageElement.style.display = 'flex';
        favoriteToggle.style.display = 'none';

        const correctCount = userAnswers.filter((answer) => answer && answer.isCorrect).length;
        const answeredCount = userAnswers.filter((answer) => answer !== null).length;
        document.getElementById('final-score').textContent = isPreview
            ? `Score So Far: ${correctCount} / ${answeredCount}`
            : `Score: ${correctCount} / ${quizData.length}`;

        backToQuizBtn.style.display = isPreview ? 'inline-block' : 'none';
        renderReviewList('all');
        saveQuizProgress();
    }

    function showQuizFromPreview() {
        isPreviewMode = false;
        backToQuizBtn.style.display = 'none';
        completionMessageElement.style.display = 'none';
        quizContentElement.style.display = 'block';
        navigationButtonsElement.style.display = 'flex';
        favoriteToggle.style.display = 'grid';
        loadQuestion();
    }

    function renderReviewList(filter) {
        currentFilter = filter;
        reviewListEl.innerHTML = '';

        filterBtns.forEach((btn) => {
            btn.classList.toggle('active', btn.dataset.filter === filter);
        });

        quizData.forEach((question, index) => {
            const userAnswer = userAnswers[index];
            const isCorrect = userAnswer && userAnswer.isCorrect;
            const isAnswered = userAnswer !== null;
            const isFavorite = favoriteQuestions.has(index);

            if (isPreviewMode && !isAnswered) return;
            if (filter === 'correct' && !isCorrect) return;
            if (filter === 'incorrect' && (isCorrect || !isAnswered)) return;
            if (filter === 'favorites' && !isFavorite) return;

            const card = document.createElement('div');
            card.classList.add('review-card');
            card.classList.add(isCorrect ? 'correct-card' : 'incorrect-card');

            let html = `<div class="review-q">${index + 1}. ${question.question}${isFavorite ? ' <span class="favorite-mark" title="Favorited">★</span>' : ''}</div>`;
            if (userAnswer) {
                html += `<div class="review-ans"><span class="ans-label">Your Answer:</span> <span class="user-ans-text ${isCorrect ? 'correct' : 'incorrect'}">${userAnswer.selected}</span></div>`;
            } else {
                html += `<div class="review-ans"><span class="ans-label">Your Answer:</span> <span class="user-ans-text incorrect">Skipped</span></div>`;
            }
            if (!isCorrect) {
                html += `<div class="review-ans"><span class="ans-label">Correct Answer:</span> ${question.correctAnswer}</div>`;
            }
            html += `<div class="review-exp">${question.explanation}</div>`;
            card.innerHTML = html;
            reviewListEl.appendChild(card);
        });
    }

    function copyReviewText() {
        let textToCopy = `Quiz Review: ${activeSessionData.title}\nFilter: ${currentFilter.toUpperCase()}\n\n`;
        let count = 0;

        quizData.forEach((question, index) => {
            const userAnswer = userAnswers[index];
            const isCorrect = userAnswer && userAnswer.isCorrect;
            const isAnswered = userAnswer !== null;
            const isFavorite = favoriteQuestions.has(index);

            if (isPreviewMode && !isAnswered) return;
            if (currentFilter === 'correct' && !isCorrect) return;
            if (currentFilter === 'incorrect' && (isCorrect || !isAnswered)) return;
            if (currentFilter === 'favorites' && !isFavorite) return;

            count += 1;
            textToCopy += `Q${index + 1}: ${question.question}${isFavorite ? ' ★' : ''}\n`;
            textToCopy += `Your Answer: ${userAnswer ? userAnswer.selected : 'Skipped'} (${isCorrect ? 'Correct' : 'Incorrect'})\n`;
            if (!isCorrect) textToCopy += `Correct Answer: ${question.correctAnswer}\n`;
            textToCopy += `Explanation: ${question.explanation}\n`;
            textToCopy += '----------------------------------------\n\n';
        });

        if (count === 0) textToCopy += 'No questions match this filter.';

        navigator.clipboard.writeText(textToCopy).then(() => {
            const originalText = copyReviewBtn.textContent;
            copyReviewBtn.textContent = 'Copied!';
            setTimeout(() => {
                copyReviewBtn.textContent = originalText;
            }, 1500);
        });
    }

    function showPanel(panelEl) {
        panelEl.style.display = 'flex';
    }

    function hidePanel(panelEl) {
        panelEl.style.display = 'none';
    }

    function downloadFile(content, fileName, contentType) {
        const blob = new Blob([content], { type: contentType });
        const anchor = document.createElement('a');
        anchor.href = URL.createObjectURL(blob);
        anchor.download = fileName;
        document.body.appendChild(anchor);
        anchor.click();
        document.body.removeChild(anchor);
        URL.revokeObjectURL(anchor.href);
    }

    function getPastSessions() {
        return JSON.parse(localStorage.getItem('pastQuizSessions')) || [];
    }

    function saveSessionData(sessionData) {
        let pastSessions = getPastSessions();
        pastSessions = pastSessions.filter((session) => session.title !== sessionData.title);
        pastSessions.push(sessionData);
        localStorage.setItem('pastQuizSessions', JSON.stringify(pastSessions));
    }

    function populateSessionList(listElement, onSelect, activeTitle = '') {
        const pastSessions = getPastSessions();
        listElement.innerHTML = '';

        if (pastSessions.length === 0) {
            listElement.innerHTML = '<li>No past quizzes found.</li>';
            return;
        }

        pastSessions.slice().reverse().forEach((session) => {
            const listItem = document.createElement('li');
            listItem.textContent = session.title;
            if (activeTitle && session.title === activeTitle) {
                listItem.style.color = 'var(--bright-blue)';
            }
            listItem.addEventListener('click', () => onSelect(session));
            listElement.appendChild(listItem);
        });
    }

    function populateAndShowPastSessions() {
        populateSessionList(
            pastSessionsListEl,
            (session) => {
                hidePanel(pastSessionsPanelEl);
                openQuiz(session);
            },
            activeSessionData ? activeSessionData.title : ''
        );
        showPanel(pastSessionsPanelEl);
    }

    function handleFileUpload(event, isMultiple) {
        const file = event.target.files[0];
        if (!file) return;

        const reader = new FileReader();
        reader.onload = (loadEvent) => {
            try {
                const data = JSON.parse(loadEvent.target.result);
                let pastSessions = getPastSessions();
                if (isMultiple) {
                    if (!Array.isArray(data)) throw new Error('File should contain an array of sessions.');
                    const sessionMap = new Map(pastSessions.map((session) => [session.title, session]));
                    data.forEach((newSession) => {
                        if (newSession.title && Array.isArray(newSession.questions)) {
                            sessionMap.set(newSession.title, newSession);
                        }
                    });
                    pastSessions = Array.from(sessionMap.values());
                    alert(`Merged ${data.length} quizzes. Total quizzes now: ${pastSessions.length}.`);
                } else {
                    if (!data.title || !Array.isArray(data.questions)) {
                        throw new Error('Invalid session JSON format.');
                    }
                    const existingIndex = pastSessions.findIndex((session) => session.title === data.title);
                    if (existingIndex > -1) pastSessions[existingIndex] = data;
                    else pastSessions.push(data);
                    alert(`Quiz "${data.title}" was successfully uploaded.`);
                }
                localStorage.setItem('pastQuizSessions', JSON.stringify(pastSessions));
            } catch (error) {
                alert('Error processing file: ' + error.message);
            }
        };

        reader.readAsText(file);
        event.target.value = '';
    }

    function buildSessionData(apiPayload, youtubeUrl) {
        const datePrefix = formatDatePrefix(new Date());
        const baseTitle = apiPayload.quiz.title.trim();

        return {
            title: `${datePrefix} ${baseTitle}`,
            questions: apiPayload.quiz.questions,
            sourceUrl: youtubeUrl,
            videoId: apiPayload.videoId,
            model: apiPayload.model,
            sourceMode: apiPayload.sourceMode,
            transcript: apiPayload.transcript,
            usage: apiPayload.usage
        };
    }

    async function generateQuizFromYouTube() {
        const youtubeUrl = youtubeUrlInput.value.trim();
        if (!youtubeUrl) {
            showStatus('Please paste a YouTube URL first.', 'error');
            return;
        }

        setGeneratingState(true, 'Generating quiz from YouTube video');

        try {
            const response = await fetch('/api/youtube-quiz/generate', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ youtubeUrl })
            });

            let payload;
            try {
                payload = await response.json();
            } catch (_) {
                payload = null;
            }

            if (!response.ok) {
                throw new Error((payload && payload.error) || 'Quiz generation failed.');
            }

            const sessionData = buildSessionData(payload, youtubeUrl);
            saveSessionData(sessionData);
            showStatus(`Generated with ${payload.model}.`, 'success');
            openQuiz(sessionData);
        } catch (error) {
            console.error('Failed to generate quiz:', error);
            showStatus(error.message || 'Failed to generate quiz.', 'error');
        } finally {
            generateBtn.disabled = false;
            youtubeUrlInput.disabled = false;
        }
    }

    function openQuiz(sessionData) {
        activeSessionData = sessionData;
        quizData = Array.isArray(sessionData.questions) ? sessionData.questions : [];
        currentQuestionIndex = 0;
        userAnswers = new Array(quizData.length).fill(null);
        favoriteQuestions = new Set();
        currentFilter = 'all';
        isPreviewMode = false;

        if (quizData.length === 0) {
            showStatus('This session has no quiz questions.', 'error');
            showHomeView();
            return;
        }

        sessionStorage.setItem('loadQuizTitle', sessionData.title);
        document.title = sessionData.title;
        quizTitleEl.textContent = sanitizeDisplayTitle(sessionData.title);
        loadUserSettings();
        revealChoicesToggleEl.checked = userSettings.hideChoicesUntilReveal;
        loadQuizProgress();
        showQuizView();
        loadQuestion();
    }

    prevButton.addEventListener('click', goToPreviousQuestion);
    nextButton.addEventListener('click', goToNextQuestion);
    favoriteToggle.addEventListener('click', toggleFavorite);
    questionCounterElement.addEventListener('click', toggleNavigator);
    navigatorClose.addEventListener('click', toggleNavigator);
    viewProgressReviewBtn.addEventListener('click', () => {
        toggleNavigator();
        showCompletion(true);
    });
    revealChoicesButton.addEventListener('click', () => {
        answerOptionsElement.style.display = 'flex';
        revealChoicesButton.style.display = 'none';
    });
    filterBtns.forEach((btn) => {
        btn.addEventListener('click', (event) => renderReviewList(event.currentTarget.dataset.filter));
    });
    copyReviewBtn.addEventListener('click', copyReviewText);
    backToQuizBtn.addEventListener('click', showQuizFromPreview);

    profileBtnEl.addEventListener('click', () => showPanel(profilePanelEl));
    document.getElementById('open-settings-btn').addEventListener('click', () => {
        hidePanel(profilePanelEl);
        showPanel(settingsPanelEl);
    });
    document.getElementById('go-home-btn').addEventListener('click', showHomeView);
    document.getElementById('view-past-sessions-btn').addEventListener('click', () => {
        hidePanel(profilePanelEl);
        populateAndShowPastSessions();
    });
    document.getElementById('download-current-session-btn').addEventListener('click', () => {
        if (!activeSessionData) return;
        downloadFile(
            JSON.stringify(activeSessionData, null, 2),
            `${activeSessionData.title.replace(/[.\s]/g, '_')}.json`,
            'application/json'
        );
    });
    document.getElementById('download-all-sessions-btn').addEventListener('click', () => {
        downloadFile(localStorage.getItem('pastQuizSessions') || '[]', 'all_past_quizzes.json', 'application/json');
    });
    document.getElementById('upload-single-session').addEventListener('change', (event) => handleFileUpload(event, false));
    document.getElementById('upload-multiple-sessions').addEventListener('change', (event) => handleFileUpload(event, true));

    revealChoicesToggleEl.addEventListener('change', (event) => {
        userSettings.hideChoicesUntilReveal = event.target.checked;
        saveUserSettings();
        if (activeSessionData) loadQuestion();
    });

    resetQuizBtn.addEventListener('click', () => {
        if (!activeSessionData) return;
        const confirmed = window.confirm('Reset this quiz? This will clear your answers, favorites, and position.');
        if (!confirmed) return;
        userAnswers = new Array(quizData.length).fill(null);
        favoriteQuestions = new Set();
        currentQuestionIndex = 0;
        currentFilter = 'all';
        localStorage.removeItem(getQuizProgressKey());
        loadQuestion();
    });

    viewPastSessionsBtn.addEventListener('click', () => {
        populateSessionList(sessionsList, (session) => {
            mainSessionsModal.style.display = 'none';
            openQuiz(session);
        });
        mainSessionsModal.style.display = 'flex';
    });

    generateBtn.addEventListener('click', generateQuizFromYouTube);
    youtubeUrlInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            generateQuizFromYouTube();
        }
    });

    mainSessionsModal.querySelector('.modal-close-btn').addEventListener('click', () => {
        mainSessionsModal.style.display = 'none';
    });
    mainSessionsModal.addEventListener('click', (event) => {
        if (event.target === mainSessionsModal) mainSessionsModal.style.display = 'none';
    });

    document.querySelectorAll('.panel-overlay').forEach((panel) => {
        panel.addEventListener('click', (event) => {
            if (event.target === panel) hidePanel(panel);
        });
    });
    document.querySelectorAll('.close-button-x').forEach((button) => {
        button.addEventListener('click', (event) => {
            hidePanel(document.getElementById(event.currentTarget.dataset.target));
        });
    });

    loadUserSettings();
    revealChoicesToggleEl.checked = userSettings.hideChoicesUntilReveal;

    const pendingQuizTitle = sessionStorage.getItem('loadQuizTitle');
    if (pendingQuizTitle) {
        const pastSessions = getPastSessions();
        const sessionToLoad = pastSessions.find((session) => session.title === pendingQuizTitle);
        if (sessionToLoad) {
            openQuiz(sessionToLoad);
        }
    }
}());
