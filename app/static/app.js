const inlineDeadlineSettings = document.getElementById('deadline-settings');
if (inlineDeadlineSettings && location.hash === '#deadline-settings') {
  inlineDeadlineSettings.open = true;
  inlineDeadlineSettings.querySelector('summary').after(...document.querySelectorAll('main > .notice'));
  requestAnimationFrame(() => {
    inlineDeadlineSettings.querySelector('summary').focus({preventScroll: true});
    inlineDeadlineSettings.scrollIntoView({block: 'start'});
  });
}
const listReturnContext = document.querySelector('[data-survey-list-context]');
if (listReturnContext) {
  document.querySelectorAll('form[method="get"]').forEach(form => {
    const destination = new URL(form.action || location.href, location.href);
    if (destination.origin !== location.origin || !/^\/(surveys(?:\/|$)|question-library(?:\/|$)|question-banks(?:\/|$))/.test(destination.pathname)) return;
    if (destination.pathname === '/surveys') return;
    listReturnContext.content.querySelectorAll('input').forEach(input => {
      if (!form.querySelector(`[name="${input.name}"]`)) form.append(input.cloneNode(true));
    });
  });
}
const errorBack = document.querySelector('[data-error-back]');
errorBack?.addEventListener('click', () => {
  let cameFromThisSite = false;
  try { cameFromThisSite = new URL(document.referrer).origin === window.location.origin; } catch (_) {}
  if (cameFromThisSite && window.history.length > 1) window.history.back();
  else window.location.assign(errorBack.dataset.fallbackUrl);
});
const confirmDialog = document.querySelector('[data-confirm-dialog]');
const responseFilters = document.querySelector('[data-response-filters]');
if (responseFilters) {
  const dateInput = responseFilters.querySelector('input[name="date"]');
  const dateToggle = responseFilters.querySelector('[data-date-filter-toggle]');
  dateInput.addEventListener('change', () => { dateToggle.checked = true; });
  responseFilters.addEventListener('submit', () => {
    if (!dateToggle.checked) dateInput.disabled = true;
  });
}
let askConfirmation = () => Promise.resolve(false);
if (confirmDialog) {
  const cancelButton = confirmDialog.querySelector('[data-confirm-cancel]');
  const proceedButton = confirmDialog.querySelector('[data-confirm-proceed]');
  let resolveConfirmation = null;
  let triggerButton = null;

  askConfirmation = ({ title, message, label, tone = 'danger' }) => {
    if (resolveConfirmation) return Promise.resolve(false);
    triggerButton = document.activeElement;
    confirmDialog.querySelector('[data-confirm-title]').textContent = title;
    confirmDialog.querySelector('[data-confirm-message]').textContent = message;
    proceedButton.textContent = label;
    proceedButton.disabled = false;
    confirmDialog.dataset.tone = tone;
    confirmDialog.showModal();
    cancelButton.focus();
    return new Promise(resolve => { resolveConfirmation = resolve; });
  };

  function finishConfirmation(confirmed) {
    const resolve = resolveConfirmation;
    if (!resolve) return;
    resolveConfirmation = null;
    confirmDialog.close();
    resolve(confirmed);
  }

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!form.matches('form[data-confirm-title]')) return;
    event.preventDefault();
    askConfirmation({
      title: form.dataset.confirmTitle,
      message: form.dataset.confirmMessage,
      label: form.dataset.confirmLabel || '确认',
      tone: form.dataset.confirmTone || 'danger'
    }).then(confirmed => {
      if (confirmed) HTMLFormElement.prototype.submit.call(form);
    });
  });

  cancelButton.addEventListener('click', () => finishConfirmation(false));
  confirmDialog.addEventListener('click', (event) => {
    if (event.target === confirmDialog) finishConfirmation(false);
  });
  confirmDialog.addEventListener('close', () => {
    if (resolveConfirmation) {
      resolveConfirmation(false);
      resolveConfirmation = null;
    }
    triggerButton?.focus();
    triggerButton = null;
  });
  proceedButton.addEventListener('click', () => finishConfirmation(true));
}

document.querySelectorAll("[data-toggle-password]").forEach((button) => {
  button.addEventListener("click", () => {
    const input = document.getElementById(button.dataset.togglePassword);
    const visible = input.type === "password";
    input.type = visible ? "text" : "password";
    const label = `${visible ? "隐藏" : "显示"}${input.id === "confirmation" ? "确认密码" : "密码"}`;
    button.setAttribute("aria-pressed", String(visible));
    button.setAttribute("aria-label", label);
    button.title = label;
  });
});

const reviewToggle = document.querySelector('[data-review-toggle]');
reviewToggle?.addEventListener('click', () => {
  const review = document.querySelector('[data-answer-review]');
  const compact = review.classList.toggle('is-compact');
  reviewToggle.textContent = compact ? '显示全部选项' : reviewToggle.dataset.compactLabel;
  reviewToggle.setAttribute('aria-pressed', String(!compact));
  document.querySelectorAll('[data-review-pages] a').forEach(link => {
    const destination = new URL(link.href);
    if (compact) destination.searchParams.delete('options');
    else destination.searchParams.set('options', 'all');
    link.href = destination.href;
  });
  document.querySelectorAll('[data-review-pages] input[name="options"]').forEach(input => {
    input.value = compact ? 'compact' : 'all';
  });
});

const questionSearch = document.querySelector('[data-question-search]');
if (questionSearch) {
  const list = document.querySelector('[data-question-list]');
  const cards = [...list.children];
  const count = document.querySelector('[data-question-search-count]');
  const empty = document.querySelector('[data-question-search-empty]');
  const pages = document.querySelector('[data-question-pages]');
  const pageSize = pages ? 20 : cards.length;
  let page = 1;
  function matchingCards() {
    const query = questionSearch.value.trim().toLocaleLowerCase();
    return cards.filter(card => !query || card.dataset.questionTitle.includes(query) || card.dataset.questionNumber === query);
  }
  function renderQuestions() {
    const matches = matchingCards();
    const pageCount = Math.max(1, Math.ceil(matches.length / pageSize));
    page = Math.min(page, pageCount);
    const shown = new Set(matches.slice((page - 1) * pageSize, page * pageSize));
    cards.forEach(card => { card.hidden = !shown.has(card); });
    count.textContent = questionSearch.value.trim() ? `找到 ${matches.length} 道题` : '';
    empty.hidden = matches.length > 0;
    if (pages) {
      pages.hidden = matches.length <= pageSize;
      pages.querySelector('[data-question-page-status]').textContent = `第 ${page} / ${pageCount} 页`;
      pages.querySelector('[data-question-prev]').disabled = page === 1;
      pages.querySelector('[data-question-next]').disabled = page === pageCount;
      pages.querySelector('[data-question-page-input]').max = pageCount;
      pages.querySelector('[data-question-page-input]').value = page;
    }
  }
  questionSearch.addEventListener('input', () => { page = 1; renderQuestions(); });
  pages?.querySelector('[data-question-prev]').addEventListener('click', () => { page--; renderQuestions(); list.scrollIntoView({ block:'start', behavior:'smooth' }); });
  pages?.querySelector('[data-question-next]').addEventListener('click', () => { page++; renderQuestions(); list.scrollIntoView({ block:'start', behavior:'smooth' }); });
  pages?.querySelector('[data-question-page-go]').addEventListener('click', () => {
    const requested = Number(pages.querySelector('[data-question-page-input]').value);
    if (!Number.isInteger(requested)) return;
    page = Math.min(Math.max(requested, 1), Math.ceil(matchingCards().length / pageSize));
    renderQuestions();
    list.scrollIntoView({ block:'start', behavior:'smooth' });
  });
  document.querySelectorAll('[data-question-view]').forEach(button => {
    button.addEventListener('click', () => {
      list.classList.toggle('is-condensed', button.dataset.questionView === 'compact');
      document.querySelectorAll('[data-question-view]').forEach(choice => choice.setAttribute('aria-pressed', String(choice === button)));
    });
  });
  list.querySelectorAll('[data-question-expand]').forEach(button => {
    button.addEventListener('click', () => {
      const card = button.closest('li');
      const expanded = card.classList.toggle('is-expanded');
      button.textContent = expanded ? '收起' : '展开操作';
      button.setAttribute('aria-expanded', String(expanded));
    });
  });
  function showAnchorQuestion() {
    const target = cards.find(card => `#${card.id}` === decodeURIComponent(location.hash));
    if (!target) return;
    questionSearch.value = '';
    page = Math.floor(cards.indexOf(target) / pageSize) + 1;
    renderQuestions();
    target.classList.add('is-expanded');
    if (new URL(location.href).searchParams.get('rules') === '1') {
      const rules = target.querySelector('.question-settings');
      if (rules) rules.open = true;
    }
    const expand = target.querySelector('[data-question-expand]');
    if (expand) { expand.textContent = '收起'; expand.setAttribute('aria-expanded', 'true'); }
    requestAnimationFrame(() => {
      target.prepend(...document.querySelectorAll('main > .notice'));
      target.setAttribute('tabindex', '-1');
      target.focus({preventScroll: true});
      target.scrollIntoView({ block:'start' });
    });
  }
  window.addEventListener('hashchange', showAnchorQuestion);
  if (location.hash) showAnchorQuestion(); else renderQuestions();
}

document.querySelectorAll('[data-view-question-rules]').forEach(link => {
  link.addEventListener('click', () => {
    const card = document.getElementById(link.hash.slice(1));
    const rules = card?.querySelector('.question-settings');
    if (rules) rules.open = true;
    if (link.hash === location.hash) window.dispatchEvent(new Event('hashchange'));
  });
});

const surveyFlow = document.querySelector("[data-survey-flow]");
const pageBackTop = document.querySelector('[data-page-back-top]');
if (pageBackTop && !surveyFlow) {
  const updateBackTop = () => { pageBackTop.hidden = window.scrollY < 650; };
  window.addEventListener('scroll', updateBackTop, { passive: true });
  window.addEventListener('resize', updateBackTop);
  pageBackTop.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
  updateBackTop();
}
if (surveyFlow) {
  const fields = [...surveyFlow.querySelectorAll("[data-question-id]")];
  const submitButton = surveyFlow.querySelector("[data-flow-submit]");
  const directSubmitButton = surveyFlow.querySelector("[data-direct-submit]");
  const status = surveyFlow.querySelector("[data-flow-status]");
  const progress = surveyFlow.querySelector("[data-progress-fill]");
  const progressTrack = surveyFlow.querySelector("[role=progressbar]");
  const error = surveyFlow.querySelector("[data-flow-error]");
  const overview = [...surveyFlow.querySelectorAll("[data-overview-question]")];
  const pageControls = surveyFlow.querySelector('[data-response-page-controls]');
  const pageStatus = surveyFlow.querySelector('[data-response-page-status]');
  const previousPage = surveyFlow.querySelector('[data-response-prev]');
  const nextPage = surveyFlow.querySelector('[data-response-next]');
  const pageSize = 8;
  let currentPage = 0;
  let reachableIds = fields.map(field => field.dataset.questionId);
  let requestVersion = 0;
  let timer;
  let submitting = false;
  const draftUrl = surveyFlow.dataset.draftUrl;
  const draftState = surveyFlow.querySelector('[data-draft-state]');
  const retryDraft = surveyFlow.querySelector('[data-retry-draft]');
  function setDraftState(message, failed = false) {
    draftState.textContent = message;
    draftState.classList.toggle('is-error', failed);
  }
  let draftTimer;
  let restoringDraft = Boolean(draftUrl);
  let draftRestored = !draftUrl;
  let draftRevision = 0;
  let draftSaving = null;
  let draftSaveQueue = Promise.resolve();
  let draftRestorePromise = Promise.resolve();
  const touchedDraftFields = new Set();
  const hasServerAnswers = [...new FormData(surveyFlow).entries()].some(([name, value]) =>
    name.startsWith('q_') && String(value).trim());
  function scheduleDraftSave() {
    clearTimeout(draftTimer);
    if (draftUrl && draftRestored && !restoringDraft && !submitting && surveyFlow.dataset.draftDirty === 'true') {
      draftTimer = setTimeout(saveDraft, 900);
    }
  }
  function draftPayload() {
    const form = new FormData(surveyFlow);
    const answers = {};
    for (const [name, value] of form.entries()) {
      if (!name.startsWith('q_')) continue;
      if (answers[name] === undefined) answers[name] = value;
      else if (Array.isArray(answers[name])) answers[name].push(value);
      else answers[name] = [answers[name], value];
    }
    return { answers, identity_mode: form.get('identity_mode') || 'named' };
  }
  async function saveDraft() {
    if (!draftUrl) return true;
    if (restoringDraft) await draftRestorePromise;
    if (!draftRestored) {
      setDraftState('尚未恢复上次答案，请先重试恢复，避免覆盖原有内容。', true);
      return false;
    }
    if (submitting) return false;
    clearTimeout(draftTimer);
    const savingRevision = draftRevision;
    let trackedSave;
    const isLatestSave = () => savingRevision === draftRevision && draftSaving === trackedSave;
    setDraftState('正在暂存…');
    try {
      const request = draftSaveQueue.then(() => fetch(draftUrl, {
        method: 'POST', headers: { 'Content-Type': 'application/json',
          'X-CSRF-Token': surveyFlow.querySelector('[name=csrf_token]').value },
        body: JSON.stringify(draftPayload())
      }));
      draftSaveQueue = request.catch(() => {});
      trackedSave = draftSaveQueue;
      draftSaving = trackedSave;
      const response = await request;
      if (!response.ok) {
        const problem = await response.json().catch(() => ({}));
        if (isLatestSave()) setDraftState(problem.error || '暂存失败，请稍后重试', true);
        return false;
      }
      if (isLatestSave()) {
        setDraftState('答案已暂存，可稍后继续填写');
        surveyFlow.dataset.draftDirty = 'false';
      }
      return true;
    } catch { if (isLatestSave()) setDraftState('暂存失败，请检查连接', true); return false; }
    finally { if (draftSaving === trackedSave) draftSaving = null; }
  }
  function restoreDraft() {
    restoringDraft = true;
    retryDraft.hidden = true;
    setDraftState('正在恢复上次答案…');
    draftRestorePromise = fetch(draftUrl).then(response => {
      if (!response.ok) throw new Error('restore failed');
      return response.json();
    }).then(saved => {
      if (!hasServerAnswers && saved.answers) {
        Object.entries(saved.answers).forEach(([name, answer]) => {
          if (touchedDraftFields.has(name)) return;
          const inputs = [...surveyFlow.elements].filter(input => input.name === name);
          const chosen = Array.isArray(answer) ? answer : [answer];
          inputs.forEach(input => {
            if (input.type === 'radio' || input.type === 'checkbox') input.checked = chosen.includes(input.value);
            else input.value = String(chosen[0] ?? '');
          });
        });
        const mode = surveyFlow.querySelector('[name=identity_mode]');
        if (mode && saved.identity_mode && !touchedDraftFields.has('identity_mode')) mode.value = saved.identity_mode;
        fields.forEach(field => {
          const clearChoice = field.querySelector('[data-clear-optional]');
          if (clearChoice) clearChoice.hidden = !field.querySelector('input[type=radio]:checked');
        });
        setDraftState('已恢复上次暂存的答案');
      } else {
        setDraftState('');
      }
      draftRestored = true;
      assess();
    }).catch(() => {
      setDraftState('上次答案恢复失败。当前输入已保留，请重试恢复。', true);
      retryDraft.hidden = false;
    }).finally(() => {
      restoringDraft = false;
      scheduleDraftSave();
    });
  }
  if (draftUrl) {
    restoreDraft();
    retryDraft.addEventListener('click', restoreDraft);
  }
  function markDraftChanged(event) {
    if (!draftUrl) return;
    if (event.target.name) touchedDraftFields.add(event.target.name);
    draftRevision++;
    surveyFlow.dataset.draftDirty = 'true';
    scheduleDraftSave();
  }
  surveyFlow.addEventListener('input', markDraftChanged);
  surveyFlow.addEventListener('change', markDraftChanged);

  function clearErrors() {
    error.textContent = "";
    fields.forEach((field) => {
      const message = field.querySelector("[data-question-error]");
      message.textContent = "";
      field.removeAttribute("aria-invalid");
    });
  }

  function showErrors(errors) {
    let first;
    fields.forEach((field) => {
      const message = errors[field.dataset.questionId];
      if (!message || !reachableIds.includes(field.dataset.questionId)) return;
      field.querySelector("[data-question-error]").textContent = message;
      field.setAttribute("aria-invalid", "true");
      if (!first) first = field;
    });
    if (first) {
      goToQuestion(first.dataset.questionId, false);
      first.scrollIntoView({ behavior: "smooth", block: "center" });
      first.querySelector("input, textarea")?.focus({ preventScroll: true });
    }
  }

  function renderQuestionPage() {
    const pages = Math.max(1, Math.ceil(reachableIds.length / pageSize));
    currentPage = Math.min(Math.max(currentPage, 0), pages - 1);
    const paged = reachableIds.length > 12;
    const visible = new Set(paged ? reachableIds.slice(currentPage * pageSize, (currentPage + 1) * pageSize) : reachableIds);
    fields.forEach(field => { field.hidden = !visible.has(field.dataset.questionId); });
    pageControls.hidden = !paged;
    if (paged) {
      const count = Math.min(pageSize, reachableIds.length - currentPage * pageSize);
      pageStatus.textContent = `第 ${currentPage + 1} 组 / 共 ${pages} 组 · 本组 ${count} 题`;
      previousPage.disabled = currentPage === 0;
      nextPage.disabled = currentPage === pages - 1;
    }
  }

  function goToQuestion(id, scroll = true) {
    const index = reachableIds.indexOf(id);
    if (index < 0) return;
    currentPage = Math.floor(index / pageSize);
    renderQuestionPage();
    if (scroll) {
      const target = fields.find(field => field.dataset.questionId === id);
      target?.setAttribute('tabindex', '-1');
      target?.focus({ preventScroll: true });
      target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  function changeQuestionPage(direction) {
    currentPage += direction;
    renderQuestionPage();
    goToQuestion(reachableIds[currentPage * pageSize]);
  }
  previousPage?.addEventListener('click', () => changeQuestionPage(-1));
  nextPage?.addEventListener('click', () => changeQuestionPage(1));

  function updateOverview(result) {
    const reachable = new Set(result.reachable);
    const currentFirst = reachableIds[currentPage * pageSize];
    reachableIds = result.reachable.filter(id => fields.some(field => field.dataset.questionId === id));
    if (currentFirst && reachableIds.includes(currentFirst)) currentPage = Math.floor(reachableIds.indexOf(currentFirst) / pageSize);
    const answered = new Set(result.answered);
    renderQuestionPage();
    overview.forEach((item) => {
      const id = item.dataset.overviewQuestion;
      item.hidden = !reachable.has(id);
      const state = item.querySelector("[data-overview-state]");
      const isAnswered = answered.has(id) && !result.errors[id];
      const isInvalid = answered.has(id) && Boolean(result.errors[id]);
      state.textContent = isInvalid ? "需修改" : isAnswered ? "已答" : fields.find(f => f.dataset.questionId === id)?.querySelector(".optional-label") ? "选答" : "未答";
      item.setAttribute('aria-label', `${item.title}，${state.textContent}`);
      item.classList.toggle("is-answered", isAnswered);
      item.classList.toggle("is-invalid", isInvalid);
    });
    const complete = result.reachable.filter(id => answered.has(id) && !result.errors[id]).length;
    const required = result.reachable.filter(id => !fields.find(f => f.dataset.questionId === id)?.querySelector(".optional-label"));
    const requiredComplete = required.filter(id => answered.has(id) && !result.errors[id]).length;
    const progressValue = required.length ? Math.round((requiredComplete / required.length) * 100) : 100;
    if (progress) progress.style.width = `${progressValue}%`;
    if (progressTrack) progressTrack.setAttribute("aria-valuenow", String(progressValue));
    const problemCount = Object.keys(result.errors).length;
    status.textContent = `已答 ${complete} / ${result.reachable.length} 题 · ${!draftRestored ? '等待恢复上次答案' : problemCount ? `待检查 ${problemCount} 题` : "可提交"}`;
  }

  async function assess(showValidation = false) {
    const version = ++requestVersion;
    try {
      const response = await fetch(surveyFlow.dataset.assessUrl, { method: "POST", body: new FormData(surveyFlow) });
      const result = await response.json();
      if (version !== requestVersion) return null;
      if (!response.ok) {
        error.textContent = result.error || "无法检查答卷，请稍后重试。";
        if (response.status === 401) window.location.href = "/login";
        return null;
      }
      updateOverview(result);
      if (showValidation) {
        clearErrors();
        if (Object.keys(result.errors).length) {
          error.textContent = "还有题目未答或答案不符合要求，请检查标出的题目。";
          showErrors(result.errors);
        }
      } else if (error.textContent && !Object.keys(result.errors).length) {
        clearErrors();
      }
      return result;
    } catch {
      if (version !== requestVersion) return null;
      error.textContent = "连接失败，请稍后重试。";
      return null;
    }
  }

  const shortcuts = surveyFlow.querySelector('[data-response-shortcuts]');
  const bottomActions = surveyFlow.querySelector('.flow-controls');
  const overviewPanel = surveyFlow.querySelector('.response-overview');
  const overviewToggle = surveyFlow.querySelector('[data-overview-toggle]');
  const compactOverview = window.matchMedia('(max-width: 800px)');
  function setOverviewOpen(open) {
    overviewPanel.classList.toggle('is-collapsed', !open);
    overviewToggle?.setAttribute('aria-expanded', String(open));
    if (overviewToggle) overviewToggle.textContent = open ? '收起题目总览 ↑' : '查看题目总览 ↓';
  }
  compactOverview.addEventListener('change', () => setOverviewOpen(!compactOverview.matches));
  setOverviewOpen(!compactOverview.matches);
  overviewToggle?.addEventListener('click', () => setOverviewOpen(overviewPanel.classList.contains('is-collapsed')));
  function updateShortcuts() {
    if (!shortcuts) return;
    const longPage = document.documentElement.scrollHeight > window.innerHeight + 600;
    const atActions = bottomActions.getBoundingClientRect().top < window.innerHeight - 70;
    shortcuts.hidden = !longPage || window.scrollY < 320 || atActions;
  }
  window.addEventListener('scroll', updateShortcuts, { passive: true });
  window.addEventListener('resize', updateShortcuts);
  updateShortcuts();
  surveyFlow.querySelector('[data-shortcut-overview]')?.addEventListener('click', () => {
    setOverviewOpen(true);
    overviewPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  surveyFlow.querySelector('[data-shortcut-top]')?.addEventListener('click', () => {
    window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  surveyFlow.querySelectorAll('[data-exit-response]').forEach(link => {
    link.addEventListener('click', async event => {
      event.preventDefault();
      const hasAnswers = [...new FormData(surveyFlow).entries()].some(([name, value]) =>
        name.startsWith('q_') && String(value).trim() !== ''
      );
      if ((hasAnswers || surveyFlow.dataset.draftDirty === 'true') && surveyFlow.dataset.preview !== 'true') {
        const confirmed = await askConfirmation({
          title: '退出填写？',
          message: '退出前会暂存当前答案，这份答卷尚未提交。',
          label: '退出填写'
        });
        if (!confirmed) return;
        clearTimeout(draftTimer);
        const saved = await saveDraft();
        if (!saved) {
          error.textContent = '暂存失败，尚未退出。请检查连接后重试，已填写的内容仍在本页。';
          return;
        }
        if (surveyFlow.dataset.draftDirty === 'true') {
          error.textContent = '还有新输入尚未暂存，请再次点击退出填写。';
          scheduleDraftSave();
          return;
        }
      }
      surveyFlow.dataset.submitting = 'true';
      window.location.assign(link.href);
    });
  });

  overview.forEach((item) => item.addEventListener("click", () => {
    if (compactOverview.matches) setOverviewOpen(false);
    goToQuestion(item.dataset.overviewQuestion);
  }));
  fields.forEach((field) => {
    const clearChoice = field.querySelector('[data-clear-optional]');
    if (clearChoice) {
      const updateClearChoice = () => { clearChoice.hidden = !field.querySelector('input[type=radio]:checked'); };
      field.addEventListener('change', updateClearChoice);
      draftRestorePromise.then(updateClearChoice);
      clearChoice.addEventListener('click', () => {
        const selected = field.querySelector('input[type=radio]:checked');
        if (!selected) return;
        selected.checked = false;
        selected.dispatchEvent(new Event('change', { bubbles: true }));
        field.querySelector('input[type=radio]')?.focus({ preventScroll: true });
      });
      updateClearChoice();
    }
    const changed = () => {
      requestVersion++;
      field.querySelector("[data-question-error]").textContent = "";
      field.removeAttribute("aria-invalid");
      clearTimeout(timer);
      timer = setTimeout(() => assess(), 180);
    };
    field.addEventListener("input", changed);
    field.addEventListener("change", changed);
  });
  surveyFlow.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting) return;
    clearTimeout(timer);
    clearTimeout(draftTimer);
    submitting = true;
    submitButton.disabled = true;
    if (restoringDraft) await draftRestorePromise;
    if (!draftRestored) {
      error.textContent = '上次答案尚未恢复，请点击“重试恢复”后再提交。';
      submitting = false;
      submitButton.disabled = false;
      return;
    }
    const result = await assess(true);
    if (result && !Object.keys(result.errors).length) {
      const confirmed = surveyFlow.dataset.preview === 'true' || await askConfirmation({
        title: '提交答卷？',
        message: '提交后这份答卷不可修改。',
        label: '确认提交',
        tone: 'primary'
      });
      if (confirmed) {
        if (draftSaving) await draftSaving.catch(() => {});
        surveyFlow.dataset.submitting = "true";
        HTMLFormElement.prototype.submit.call(surveyFlow);
        return;
      }
    }
    submitting = false;
    submitButton.disabled = false;
    scheduleDraftSave();
  });
  directSubmitButton?.addEventListener("click", () => surveyFlow.requestSubmit());
  assess().then(() => {
    if (surveyFlow.dataset.errorQuestion && surveyFlow.dataset.initialError) {
      showErrors({ [surveyFlow.dataset.errorQuestion]: surveyFlow.dataset.initialError });
    }
  });
}

document.querySelectorAll("[data-copy-url]").forEach((button) => {
  button.addEventListener("click", async () => {
    const feedback = button.parentElement.querySelector("[data-copy-feedback]");
    try {
      await navigator.clipboard.writeText(button.dataset.copyUrl);
      feedback.textContent = "链接已复制";
    } catch {
      feedback.textContent = "未能自动复制，请手动复制下方链接。";
      const link = document.createElement('input');
      link.type = 'text';
      link.readOnly = true;
      link.value = button.dataset.copyUrl;
      link.setAttribute('aria-label', '填写链接，供手动复制');
      link.style.width = '100%';
      link.style.minWidth = '0';
      feedback.append(link);
      link.focus();
      link.select();
    }
  });
});

const jumpForm = document.querySelector("[data-jump-form]");
if (jumpForm) {
  const multi = jumpForm.querySelector("[data-jump-multi]");
  const error = jumpForm.querySelector("[data-jump-error]");
  const targetSearch = jumpForm.querySelector('[data-jump-target-search]');
  if (targetSearch) {
    targetSearch.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.isComposing) event.preventDefault();
    });
    const targets = [...jumpForm.querySelectorAll('[data-jump-target-grid] .jump-target')];
    const empty = jumpForm.querySelector('[data-jump-target-empty]');
    targetSearch.addEventListener('input', () => {
      const query = targetSearch.value.trim().toLocaleLowerCase();
      let shown = 0;
      targets.forEach(target => {
        target.hidden = Boolean(query) && target.dataset.targetNumber !== query && !target.dataset.targetTitle.includes(query);
        if (!target.hidden) shown++;
      });
      empty.hidden = shown > 0;
    });
  }
  jumpForm.addEventListener("submit", (event) => {
    if (multi && !multi.querySelector("input:checked")) {
      event.preventDefault();
      error.textContent = "请至少选择一个用于匹配的选项。";
      error.hidden = false;
      multi.scrollIntoView({ block: "center" });
    }
  });
  multi?.addEventListener("change", () => { error.hidden = true; error.textContent = ""; });
}

document.querySelectorAll("[data-warn-unsaved]").forEach((form) => {
  if (form.dataset.preview === 'true') return;
  function fingerprint() {
    const entries = [...new FormData(form).entries()].filter(([name]) => name !== "csrf_token")
      .map(([name, value]) => [name, name === "options" ? String(value).split('\n').map(v => v.trim()).filter(Boolean).join('\n') : value]);
    const selectionMode = form.querySelector("[data-selection-mode]");
    if (selectionMode) entries.push(["selection_mode", selectionMode.value]);
    return JSON.stringify(entries);
  }
  const original = fingerprint();
  let submitted = false;
  form.addEventListener("submit", (event) => { submitted = !event.defaultPrevented; });
  form.querySelectorAll("[data-discard-changes]").forEach((link) => {
    link.addEventListener("click", () => { form.dataset.submitting = "true"; });
  });
  window.addEventListener("beforeunload", (event) => {
    if (form.dataset.draftUrl && form.dataset.draftDirty !== 'true') return;
    if (submitted || form.dataset.submitting === "true" || fingerprint() === original) return;
    event.preventDefault();
    event.returnValue = "";
  });
});

const surveyFilters = document.querySelector("[data-survey-filters]");
if (surveyFilters) {
  const search = surveyFilters.querySelector("[data-survey-search]");
  const status = surveyFilters.querySelector("[data-survey-status]");
  const items = [...document.querySelectorAll("[data-survey-item]")];
  const empty = document.querySelector("[data-filter-empty]");
  const pages = document.querySelector('[data-survey-pages]');
  const pageSize = pages ? 20 : items.length;
  let page = 1;
  function filterSurveys() {
    const query = search.value.trim().toLocaleLowerCase();
    const matches = items.filter(item => item.dataset.title.includes(query) && (!status.value || item.dataset.status === status.value));
    const pageCount = Math.max(1, Math.ceil(matches.length / pageSize));
    page = Math.min(page, pageCount);
    const shown = new Set(matches.slice((page - 1) * pageSize, page * pageSize));
    items.forEach((item) => {
      item.hidden = !shown.has(item);
    });
    empty.hidden = matches.length !== 0;
    if (pages) {
      pages.hidden = matches.length <= pageSize;
      pages.querySelector('[data-survey-page-status]').textContent = `第 ${page} / ${pageCount} 页`;
      pages.querySelector('[data-survey-prev]').disabled = page === 1;
      pages.querySelector('[data-survey-next]').disabled = page === pageCount;
      pages.querySelector('[data-survey-page-input]').max = pageCount;
      pages.querySelector('[data-survey-page-input]').value = page;
    }
  }
  search.addEventListener("input", () => { page = 1; filterSurveys(); });
  status.addEventListener("change", () => { page = 1; filterSurveys(); });
  pages?.querySelector('[data-survey-prev]').addEventListener('click', () => { page--; filterSurveys(); surveyFilters.scrollIntoView({ block:'start', behavior:'smooth' }); });
  pages?.querySelector('[data-survey-next]').addEventListener('click', () => { page++; filterSurveys(); surveyFilters.scrollIntoView({ block:'start', behavior:'smooth' }); });
  pages?.querySelector('[data-survey-page-go]').addEventListener('click', () => {
    const requested = Number(pages.querySelector('[data-survey-page-input]').value);
    if (!Number.isInteger(requested)) return;
    const matches = items.filter(item => item.dataset.title.includes(search.value.trim().toLocaleLowerCase()) && (!status.value || item.dataset.status === status.value));
    page = Math.min(Math.max(requested, 1), Math.max(1, Math.ceil(matches.length / pageSize)));
    filterSurveys();
    surveyFilters.scrollIntoView({ block:'start', behavior:'smooth' });
  });
  filterSurveys();
}

// Keep one server-facing value while making the date and time quick to choose.
document.querySelectorAll('[data-deadline-picker]').forEach(picker => {
  const value = picker.querySelector('[data-deadline-value]');
  const ui = picker.querySelector('[data-deadline-ui]');
  const date = picker.querySelector('[data-deadline-date]');
  const time = picker.querySelector('[data-deadline-time]');
  const clear = picker.querySelector('[data-deadline-clear]');
  function todayInChina() {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'
    }).formatToParts(new Date());
    const part = type => parts.find(item => item.type === type).value;
    return `${part('year')}-${part('month')}-${part('day')}`;
  }
  function sync(notify = true) {
    value.value = date.value ? `${date.value}T${time.value || '23:59'}` : '';
    clear.setAttribute('aria-pressed', String(!date.value));
    time.closest('label').hidden = !date.value;
    picker.classList.toggle('is-unlimited', !date.value);
    if (notify) value.dispatchEvent(new Event('input', { bubbles: true }));
  }
  date.value = value.value.slice(0, 10);
  time.value = value.value.slice(11, 16) || '23:59';
  date.min = date.value && date.value < todayInChina() ? date.value : todayInChina();
  picker.querySelectorAll('[data-deadline-days]').forEach(button => {
    button.addEventListener('click', () => {
      const selected = new Date(`${todayInChina()}T00:00:00Z`);
      selected.setUTCDate(selected.getUTCDate() + Number(button.dataset.deadlineDays));
      date.value = selected.toISOString().slice(0, 10);
      time.value = '23:59';
      sync();
    });
  });
  clear.addEventListener('click', () => { date.value = ''; sync(); });
  date.addEventListener('change', () => sync());
  time.addEventListener('change', () => {
    if (!date.value) date.value = todayInChina();
    sync();
  });
  value.hidden = true;
  ui.hidden = false;
  sync(false);
});

const questionEditor = document.querySelector('[data-question-form]');
if (questionEditor) {
  const selector = questionEditor.querySelector('#question-type');
  const typeChoices = questionEditor.querySelector('[data-type-choices]');
  const options = questionEditor.querySelector('#options');
  const optionEditor = questionEditor.querySelector('[data-options-editor]');
  const optionRows = questionEditor.querySelector('[data-options-rows]');
  const batch = questionEditor.querySelector('[data-batch-options]');
  const preview = document.querySelector('[data-question-preview]');
  let batching = false;
  function optionMarker(position) {
    if (questionEditor.dataset.optionStyle !== 'letters') return String(position);
    let label = '';
    while (position) {
      position--;
      label = String.fromCharCode(65 + position % 26) + label;
      position = Math.floor(position / 26);
    }
    return label;
  }
  function writeOptions() {
    options.value = [...optionRows.querySelectorAll('input')].map(input=>input.value).join('\n');
    options.dispatchEvent(new Event('input', {bubbles:true}));
  }
  function renderRows(values) {
    optionRows.replaceChildren();
    while(values.length<2) values.push('');
    values.forEach((text,index)=> {
      const row=document.createElement('div'); row.className='option-editor-row';
      const number=document.createElement('span'); number.textContent=optionMarker(index+1);
      const input=document.createElement('input'); input.type='text'; input.value=text; input.placeholder=`选项 ${index+1}`; input.setAttribute('aria-label',`选项 ${index+1}`);
      input.addEventListener('input',writeOptions);
      input.addEventListener('keydown', event => {
        if (event.key !== 'Enter' || event.isComposing || event.keyCode === 229) return;
        event.preventDefault();
        const next = row.nextElementSibling?.querySelector('input');
        if (next) next.focus();
        else if (input.value.trim()) questionEditor.querySelector('[data-add-option]').click();
      });
      const move=document.createElement('button'); move.type='button'; move.textContent='↑'; move.title='上移选项'; move.setAttribute('aria-label',`上移选项 ${index+1}`); move.disabled=index===0;
      move.addEventListener('click',()=> { const vals=[...optionRows.querySelectorAll('input')].map(i=>i.value); [vals[index-1],vals[index]]=[vals[index],vals[index-1]]; renderRows(vals);writeOptions();optionRows.children[index-1].querySelector('input').focus(); });
      const remove=document.createElement('button'); remove.type='button'; remove.textContent='×'; remove.title='删除选项'; remove.setAttribute('aria-label',`删除选项 ${index+1}`); remove.disabled=values.length<=2;
      remove.addEventListener('click',()=> { const vals=[...optionRows.querySelectorAll('input')].map(i=>i.value); vals.splice(index,1);renderRows(vals);writeOptions();optionRows.children[Math.min(index, vals.length-1)].querySelector('input').focus(); });
      row.append(number,input,move,remove); optionRows.append(row);
    });
  }
  function updatePreview() {
    optionRows.querySelectorAll('.option-editor-row > span').forEach((number,index)=> {number.textContent=optionMarker(index+1);});
    typeChoices.querySelectorAll('button').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.questionType===selector.value)));
    preview.querySelector('[data-preview-type]').textContent=selector.selectedOptions[0].textContent;
    const required=questionEditor.querySelector('[name=required]');
    preview.querySelector('[data-preview-title]').textContent=(questionEditor.querySelector('[name=title]').value || '题目内容') + (!required.checked && !required.disabled ? '（选答）' : '');
    const area=preview.querySelector('[data-preview-options]'); area.replaceChildren();
    if(['single_choice','multiple_choice'].includes(selector.value)) {
      const values=options.value.split('\n').map(v=>v.trim()).filter(Boolean);
      (values.length?values:['选项 1','选项 2']).forEach((value,index)=> { const row=document.createElement('div');row.className='preview-option';const icon=document.createElement('span');icon.textContent=selector.value==='single_choice'?'○':'□';const marker=document.createElement('span');marker.className='preview-option-label';marker.textContent=`${optionMarker(index+1)}.`;row.append(icon,marker,document.createTextNode(value));area.append(row); });
    } else {
      const input=document.createElement(selector.value==='text'?'textarea':'input'); input.disabled=true;input.placeholder=selector.value==='text'?'填写文字回答':'填写数字';area.append(input);
    }
    const val=name=>questionEditor.querySelector(`[name="${name}"]`).value;
    const rules=[];
    if(selector.value==='multiple_choice') {
      const exact=questionEditor.querySelector('[data-selection-mode]').value==='exact';
      if(exact && val('exact_select')) rules.push(`需选 ${val('exact_select')} 项`);
      if(!exact) { if(val('min_select')) rules.push(`至少选 ${val('min_select')} 项`); if(val('max_select')) rules.push(`最多选 ${val('max_select')} 项`); }
    } else if(selector.value==='text') { if(val('min_length')) rules.push(`至少填写 ${val('min_length')} 字`);if(val('max_length')) rules.push(`最多填写 ${val('max_length')} 字`); }
    else if(selector.value==='number') {if(val('min_value')) rules.push(`不小于 ${val('min_value')}`);if(val('max_value')) rules.push(`不大于 ${val('max_value')}`);if(questionEditor.querySelector('[name=integer_only]').checked) rules.push('整数');}
    preview.querySelector('[data-preview-rules]').textContent=rules.join(' · ');
  }
  typeChoices.querySelectorAll('button').forEach(button=>button.addEventListener('click',()=> {selector.value=button.dataset.questionType;selector.dispatchEvent(new Event('change',{bubbles:true}));}));
  questionEditor.querySelector('[data-add-option]').addEventListener('click',()=> {
    if(batching) batch.click();
    const vals=[...optionRows.querySelectorAll('input')].map(i=>i.value);vals.push('');renderRows(vals);writeOptions();optionRows.lastElementChild.querySelector('input').focus();
  });
  batch.addEventListener('click',()=> { batching=!batching;options.hidden=!batching;optionRows.hidden=batching;batch.textContent=batching?'完成批量输入':'批量输入';if(batching){options.placeholder='每行一个选项';options.focus();}else{renderRows(options.value.split('\n'));writeOptions();} });
  renderRows(options.value ? options.value.split('\n') : ['', '']);
  selector.hidden=true; typeChoices.hidden=false; options.hidden=true;optionEditor.hidden=false;preview.hidden=false;
  questionEditor.addEventListener('input',updatePreview);questionEditor.addEventListener('change',updatePreview);updatePreview();
}
