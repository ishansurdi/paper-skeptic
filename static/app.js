const form = document.getElementById('analyze-form');
const input = document.getElementById('arxiv-url');
const composer = document.getElementById('composer');
const loading = document.getElementById('loading');
const loadingText = document.getElementById('loading-text');
const results = document.getElementById('results');
const errorMessage = document.getElementById('error-message');
const summaryEl = document.getElementById('summary');
const doubtsList = document.getElementById('doubts-list');
const doubtsNote = document.getElementById('doubts-note');
const analyzeAnother = document.getElementById('analyze-another');

let loadingTimer = null;
let loadingStep = 0;

function formatCategoryLabel(category) {
  return category.replaceAll('_', ' ');
}

function clearLoadingAnimation() {
  if (loadingTimer) {
    window.clearInterval(loadingTimer);
    loadingTimer = null;
  }
}

function resetResultContent() {
  summaryEl.textContent = '';
  doubtsList.innerHTML = '';
  doubtsNote.hidden = true;
  doubtsNote.textContent = '';
}

function showLoadingState() {
  composer.hidden = true;
  results.hidden = true;
  errorMessage.textContent = '';

  loading.hidden = false;
  loadingStep = 0;
  loadingText.textContent = 'reading';

  clearLoadingAnimation();
  loadingTimer = window.setInterval(() => {
    loadingStep = (loadingStep + 1) % 4;
    loadingText.textContent = `reading${'.'.repeat(loadingStep)}`;
  }, 520);
}

function showComposerWithError(message) {
  clearLoadingAnimation();
  loading.hidden = true;
  results.hidden = true;
  composer.hidden = false;
  errorMessage.textContent = message;
  input.focus();
}

function showComposerClean() {
  clearLoadingAnimation();
  loading.hidden = true;
  results.hidden = true;
  composer.hidden = false;
  errorMessage.textContent = '';
  loadingText.textContent = 'reading';
  resetResultContent();
}

function renderResult(resultData) {
  const summary = typeof resultData?.summary === 'string'
    ? resultData.summary
    : 'No summary returned.';

  const doubts = Array.isArray(resultData?.doubts) ? resultData.doubts : [];
  const doubtsFound = Number.isInteger(resultData?.doubts_found)
    ? resultData.doubts_found
    : doubts.length;

  summaryEl.textContent = summary;
  doubtsList.innerHTML = '';

  doubts.forEach((doubt) => {
    const item = document.createElement('li');
    item.className = 'doubt-item';

    const category = document.createElement('span');
    category.className = 'doubt-category';
    category.textContent = formatCategoryLabel(doubt.category || 'unspecified');

    const headline = document.createElement('p');
    headline.className = 'doubt-headline';
    headline.textContent = doubt.headline || 'Untitled doubt';

    const evidence = document.createElement('p');
    evidence.className = 'doubt-evidence';
    evidence.textContent = doubt.evidence || 'No evidence provided.';

    item.append(category, headline, evidence);
    doubtsList.appendChild(item);
  });

  if (doubtsFound < 3) {
    doubtsNote.hidden = false;
    doubtsNote.textContent =
      `only ${doubtsFound} substantive doubts surfaced — this paper may be more honest than most.`;
  } else {
    doubtsNote.hidden = true;
    doubtsNote.textContent = '';
  }
}

function showResults() {
  clearLoadingAnimation();
  loading.hidden = true;
  composer.hidden = true;
  results.hidden = false;
}

async function submitForAnalysis(arxivUrl) {
  let response;
  try {
    response = await fetch('/analyze', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ arxiv_url: arxivUrl }),
    });
  } catch {
    throw new Error('NETWORK_UNREACHABLE');
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    if (response.status === 400) {
      const detail = payload?.detail || 'invalid request';
      throw new Error(`BAD_REQUEST:${detail}`);
    }
    if (response.status === 429) {
      const detail = payload?.detail || 'rate limit exceeded. try again later.';
      throw new Error(`RATE_LIMIT:${detail}`);
    }
    if (response.status === 504) {
      const detail = payload?.detail || 'request timed out. try again.';
      throw new Error(`TIMEOUT_ERROR:${detail}`);
    }
    if (response.status >= 500) {
      throw new Error('SERVER_ERROR');
    }

    const detail = payload?.detail || 'Request failed. Please try another arXiv link.';
    throw new Error(`OTHER_ERROR:${detail}`);
  }

  return payload;
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();

  const arxivUrl = input.value.trim();
  if (!arxivUrl) {
    showComposerWithError('please provide an arxiv link or paper id.');
    return;
  }

  showLoadingState();

  try {
    const responsePayload = await submitForAnalysis(arxivUrl);
    renderResult(responsePayload?.result || {});
    showResults();
  } catch (error) {
    const message = String(error?.message || 'OTHER_ERROR:Request failed');

    if (message === 'NETWORK_UNREACHABLE') {
      showComposerWithError('network error: server unreachable.');
      return;
    }

    if (message.startsWith('BAD_REQUEST:')) {
      showComposerWithError(message.replace('BAD_REQUEST:', ''));
      return;
    }

    if (message.startsWith('RATE_LIMIT:')) {
      showComposerWithError(message.replace('RATE_LIMIT:', ''));
      return;
    }

    if (message === 'SERVER_ERROR') {
      showComposerWithError('something broke. try again.');
      return;
    }

    if (message.startsWith('TIMEOUT_ERROR:')) {
      showComposerWithError(message.replace('TIMEOUT_ERROR:', ''));
      return;
    }

    if (message.startsWith('OTHER_ERROR:')) {
      showComposerWithError(message.replace('OTHER_ERROR:', ''));
      return;
    }

    showComposerWithError('something broke. try again.');
  }
});

analyzeAnother.addEventListener('click', (event) => {
  event.preventDefault();
  showComposerClean();
  form.reset();
  input.focus();
});
