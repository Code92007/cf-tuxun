const state = {
  user: null,
  csrf: "",
  authMode: "login",
  view: "dashboard",
  solo: { filters: null, question: null, answerMode: "contest", rated: false, scoringMode: "classic", resolved: true, settling: false, timed: false, timeLimit: 0, attemptsUsed: 0, maxAttempts: 1, round: 0, startedAt: 0, timer: null, retryTimer: null },
  daily: { challenge: null, timer: null, startedAt: 0, secondsLeft: 0, settling: false },
  battle: { code: null, answerMode: "contest", poll: null, polling: false, roundSeen: 0, current: null },
  uploadData: "",
};

const MAX_SUBMISSION_IMAGE_BYTES = 3 * 1024 * 1024;

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[ch]);
}

function toast(message, type = "") {
  const el = $("#toast");
  el.textContent = message;
  el.className = `toast show ${type}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.className = "toast"; }, 2800);
}

async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: { Accept: "application/json" } };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  if (init.method !== "GET" && state.csrf) init.headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(path, init);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.error || `请求失败 (${response.status})`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function setButtonBusy(button, busy, label = "处理中...") {
  if (!button) return;
  if (busy) {
    button.dataset.original = button.textContent;
    button.textContent = label;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.original || button.textContent;
    button.disabled = false;
  }
}

function selectSegment(container, button) {
  $$("button", container).forEach(el => el.classList.toggle("active", el === button));
}

function selectedValue(selector) {
  return $(`${selector} button.active`)?.dataset.value;
}

function renderUser() {
  if (!state.user) return;
  const u = state.user;
  $("#side-username").textContent = u.username;
  $("#side-avatar").textContent = u.username.slice(0, 1).toUpperCase();
  $("#side-rating").textContent = u.rating;
  $("#top-score").textContent = u.totalScore.toLocaleString();
  $("#metric-score").textContent = u.totalScore.toLocaleString();
  $("#metric-rating").textContent = u.rating;
  $("#metric-accuracy").textContent = `${u.accuracy}%`;
  $("#metric-attempts").textContent = `${u.attempts} 次作答`;
  $("#metric-streak").textContent = u.bestStreak;
  $("#rating-tier").textContent = ratingTier(u.rating);
  $("#admin-nav").classList.toggle("hidden", !u.isAdmin);
  $("#permissions-nav").classList.toggle("hidden", !u.isSuperAdmin);
}

function ratingTier(rating) {
  if (rating >= 2400) return "Grandmaster";
  if (rating >= 2100) return "Master";
  if (rating >= 1900) return "Candidate Master";
  if (rating >= 1600) return "Expert";
  if (rating >= 1400) return "Specialist";
  if (rating >= 1200) return "Pupil";
  return "Newbie";
}

function showAuthenticated(authenticated) {
  $("#auth-screen").classList.toggle("hidden", authenticated);
  $("#app-shell").classList.toggle("hidden", !authenticated);
}

const viewMeta = {
  dashboard: ["OVERVIEW", "属于算法竞赛的图寻"],
  solo: ["SOLO QUIZ", "单人图寻"],
  daily: ["DAILY FIVE", "每日挑战"],
  battle: ["VERSUS", "双人对战"],
  submit: ["CONTRIBUTE", "投稿线索"],
  admin: ["MODERATION", "审核投稿"],
  permissions: ["ACCESS CONTROL", "权限管理"],
  leaderboard: ["RANKING", "排行榜"],
};

function showView(name) {
  state.view = name;
  $$(".view").forEach(el => el.classList.toggle("active", el.id === `view-${name}`));
  $$(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.view === name));
  $("#page-eyebrow").textContent = viewMeta[name][0];
  $("#page-title").textContent = viewMeta[name][1];
  $(".sidebar").classList.remove("open");
  if (name === "leaderboard") loadLeaderboard();
  if (name === "daily") loadDaily();
  if (name === "submit") loadMySubmissions();
  if (name === "admin") loadAdminSubmissions();
  if (name === "admin") loadOpenCandidates();
  if (name === "permissions") loadPermissionUsers();
  if (name !== "battle" && state.battle.poll) stopBattlePoll();
  if (name === "battle" && state.battle.code) startBattlePoll();
  if (name !== "daily") stopDailyTimer();
}

function setAuthMode(mode) {
  state.authMode = mode;
  $$('[data-auth-mode]').forEach(btn => btn.classList.toggle("active", btn.dataset.authMode === mode));
  $("#auth-submit").textContent = mode === "login" ? "登录" : "创建账户";
  $("#auth-password").autocomplete = mode === "login" ? "current-password" : "new-password";
  $("#auth-hint").textContent = mode === "login" ? "登录后才能累计得分和 rating。" : "用户名支持字母、数字和下划线；密码至少 8 位。";
}

async function submitAuth(event) {
  event.preventDefault();
  const button = $("#auth-submit");
  setButtonBusy(button, true);
  try {
    const payload = await api(`/api/auth/${state.authMode}`, {
      method: "POST",
      body: { username: $("#auth-username").value, password: $("#auth-password").value },
    });
    state.user = payload.user;
    state.csrf = payload.csrf;
    showAuthenticated(true);
    renderUser();
    showView("dashboard");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

async function logout() {
  try { await api("/api/auth/logout", { method: "POST", body: {} }); } catch (_) { /* expire locally */ }
  stopBattlePoll();
  state.user = null;
  state.csrf = "";
  showAuthenticated(false);
  $("#auth-password").value = "";
}

function soloFilters() {
  return {
    difficulty: selectedValue("#solo-difficulty") || "medium",
    questionMode: selectedValue("#solo-question-mode") || "standard",
    contestMin: $("#solo-contest-min").value,
    contestMax: $("#solo-contest-max").value,
    yearMin: $("#solo-year-min").value,
    yearMax: $("#solo-year-max").value,
    roundTypes: $$(".round-filter input:checked").map(el => el.value),
  };
}

function updateQuestionModeHints() {
  $("#solo-open-rule").classList.toggle("hidden", selectedValue("#solo-question-mode") !== "open");
  $("#battle-open-rule").classList.toggle("hidden", selectedValue("#battle-question-mode") !== "open");
  updateScoringCompatibility();
}

function updateSoloModeControls() {
  const rated = selectedValue("#solo-mode") === "true";
  const difficultyButtons = $$("#solo-difficulty button");
  if (rated) selectSegment($("#solo-difficulty"), $("#solo-difficulty button[data-value='all']"));
  difficultyButtons.forEach(button => { button.disabled = rated; });
  $("#solo-rating-rule").classList.toggle("hidden", !rated);
  if (rated) {
    selectSegment($("#solo-timing"), $("#solo-timing button[data-value='true']"));
    $("#solo-time-limit").value = "120";
  }
  $$("#solo-timing button").forEach(button => { button.disabled = rated; });
  $("#solo-time-limit").disabled = rated;
  updateSoloTimingControls();
  updateScoringCompatibility();
}

function updateSoloTimingControls() {
  const timed = selectedValue("#solo-timing") === "true";
  const distance = selectedValue("#solo-scoring-mode") === "distance";
  $("#solo-time-limit-wrap").classList.toggle("hidden", !timed);
  $("#solo-attempt-rule").classList.toggle("hidden", !timed || distance);
}

function updateSoloScoringControls() {
  const distance = selectedValue("#solo-scoring-mode") === "distance";
  $("#solo-scoring-hint").classList.toggle("hidden", !distance);
  updateSoloTimingControls();
}

function updateScoringCompatibility() {
  const soloBrain = selectedValue("#solo-difficulty") === "brain";
  const soloOpen = selectedValue("#solo-question-mode") === "open";
  const soloDistance = $("#solo-scoring-mode button[data-value='distance']");
  soloDistance.disabled = soloBrain || soloOpen;
  if ((soloBrain || soloOpen) && selectedValue("#solo-scoring-mode") === "distance") {
    selectSegment($("#solo-scoring-mode"), $("#solo-scoring-mode button[data-value='classic']"));
    updateSoloScoringControls();
    toast(soloOpen ? "开放多解只支持传统对错，已自动切换" : "最强大脑不会提供距离分，已切换为传统对错");
  }
  const battleBrain = selectedValue("#battle-difficulty") === "brain";
  const battleOpen = selectedValue("#battle-question-mode") === "open";
  const battleDistance = $("#battle-scoring-mode button[data-value='distance']");
  battleDistance.disabled = battleBrain || battleOpen;
  if ((battleBrain || battleOpen) && selectedValue("#battle-scoring-mode") === "distance") {
    selectSegment($("#battle-scoring-mode"), $("#battle-scoring-mode button[data-value='classic']"));
    updateBattleScoringControls();
    toast(battleOpen ? "开放多解只支持抢答模式，已自动切换" : "最强大脑不会提供距离分，已切换为抢答模式");
  }
}

async function startSolo() {
  state.solo.filters = soloFilters();
  state.solo.rated = selectedValue("#solo-mode") === "true";
  state.solo.scoringMode = selectedValue("#solo-scoring-mode") || "classic";
  state.solo.timed = state.solo.rated || selectedValue("#solo-timing") === "true";
  state.solo.timeLimit = state.solo.rated ? 120 : Number($("#solo-time-limit").value || 120);
  state.solo.round = 0;
  $("#solo-setup").classList.add("hidden");
  $("#solo-game").classList.remove("hidden");
  await nextSoloQuestion();
}

async function nextSoloQuestion() {
  const button = $("#solo-start");
  try {
    setButtonBusy(button, true, "正在抽题...");
    const payload = await api("/api/quiz/next", {
      method: "POST",
      body: {
        filters: state.solo.filters,
        rated: state.solo.rated,
        scoringMode: state.solo.scoringMode,
        timed: state.solo.timed,
        timeLimit: state.solo.timeLimit,
      },
    });
    state.solo.question = payload.question;
    state.solo.rated = payload.rated;
    state.solo.scoringMode = payload.question.scoringMode;
    state.solo.resolved = false;
    state.solo.settling = false;
    state.solo.timed = payload.question.timed;
    state.solo.timeLimit = payload.question.timeLimit;
    state.solo.maxAttempts = payload.question.maxAttempts;
    state.solo.attemptsUsed = 0;
    state.solo.round += 1;
    state.solo.startedAt = Date.now();
    $("#solo-round-label").textContent = `第 ${state.solo.round} 题`;
    $("#solo-progress").style.width = `${Math.min(100, (state.solo.round % 10 || 10) * 10)}%`;
    $("#solo-clue").src = `${payload.question.clueUrl}?v=${Date.now()}`;
    $("#solo-result").className = "result-strip hidden";
    $("#solo-result").innerHTML = "";
    $("#solo-answer-form").classList.remove("hidden");
    $("#abandon-solo").classList.remove("hidden");
    $("#abandon-solo").disabled = false;
    $("#solo-answer-form .submit-answer").disabled = false;
    $("#solo-answer-form .submit-answer").textContent = state.solo.scoringMode === "distance" ? "锁定答案" : "提交答案";
    $("#solo-mode-status").textContent = `${payload.question.openMode ? "开放多解" : "普通题"} · ${state.solo.rated ? "Rating 模式" : "娱乐模式"} · ${state.solo.scoringMode === "distance" ? "距离积分" : "传统对错"}`;
    $("#solo-attempts-status").classList.toggle("hidden", !state.solo.timed || state.solo.scoringMode === "distance");
    $("#solo-attempts-status").textContent = `0 / ${state.solo.maxAttempts} 次`;
    $("#solo-timer-label").textContent = state.solo.timed ? "剩余" : "用时";
    $("#solo-answer-form").reset();
    renderDivisionChoices("solo", payload.question);
    setSoloAnswerMode("contest");
    $("#solo-answer-mode").classList.toggle("hidden", state.solo.scoringMode === "distance");
    $("#solo-contest-answer").focus();
    clearInterval(state.solo.timer);
    state.solo.timer = setInterval(updateSoloTimer, 250);
    updateSoloTimer();
  } catch (error) {
    state.solo.question = null;
    state.solo.resolved = true;
    $("#solo-setup").classList.remove("hidden");
    $("#solo-game").classList.add("hidden");
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

function updateSoloTimer() {
  const elapsed = Math.floor((Date.now() - state.solo.startedAt) / 1000);
  const shown = state.solo.timed ? Math.max(0, state.solo.timeLimit - elapsed) : elapsed;
  $("#solo-timer").textContent = `${String(Math.floor(shown / 60)).padStart(2, "0")}:${String(shown % 60).padStart(2, "0")}`;
  if (state.solo.timed && shown <= 0 && !state.solo.resolved && !state.solo.settling) timeoutSoloQuestion();
}

function renderDivisionChoices(prefix, question) {
  const wrap = $(`#${prefix}-division-wrap`);
  wrap.classList.toggle("hidden", !question.needsDivision);
  const input = $(`#${prefix}-division`);
  input.value = "";
  input.title = "可留空；同一 Round 有多个组别时填写。1=Div. 1，2=Div. 2，3=Div. 3，4=Div. 4，12=Div. 1 + Div. 2，E=Educational";
}

function setSoloAnswerMode(mode) {
  if (state.solo.scoringMode === "distance") mode = "contest";
  state.solo.answerMode = mode;
  $$('[data-answer-mode]').forEach(btn => btn.classList.toggle("active", btn.dataset.answerMode === mode));
  $("#solo-contest-fields").classList.toggle("hidden", mode !== "contest");
  $("#solo-round-fields").classList.toggle("hidden", mode !== "round");
}

function answerPayload(prefix, mode) {
  return {
    answerMode: mode,
    contestAnswer: $(`#${prefix}-contest-answer`).value,
    roundNumber: $(`#${prefix}-round-number`).value,
    roundIndex: $(`#${prefix}-round-index`).value,
    division: $(`#${prefix}-division`).value,
  };
}

function solutionText(solution) {
  return solution.answers.map(a => a.round ? `${escapeHtml(a.contest)} / ${escapeHtml(a.round)}` : escapeHtml(a.contest)).join("<br>");
}

function statusText(status) {
  return { pending: "待审核", approved: "已通过", rejected: "未通过" }[status] || status;
}

function submissionItem(item) {
  const media = item.imageUrl
    ? `<div class="submission-thumb"><img src="${escapeHtml(item.imageUrl)}" alt="投稿裁图"></div>`
    : `<div class="submission-thumb">TEXT</div>`;
  return `<article class="submission-item ${escapeHtml(item.status)}">${media}<div class="submission-copy"><strong>${escapeHtml(item.answer)}</strong><span class="status-label">${statusText(item.status)}${item.suggestedOpen ? " · 开放题" : ""}</span>${item.acceptedAnswers ? `<p>其他答案：${escapeHtml(item.acceptedAnswers)}</p>` : ""}${item.textClue ? `<p>${escapeHtml(item.textClue)}</p>` : ""}${item.note ? `<p>${escapeHtml(item.note)}</p>` : ""}${item.reviewNote ? `<p>审核：${escapeHtml(item.reviewNote)}</p>` : ""}</div></article>`;
}

async function loadMySubmissions() {
  const container = $("#my-submissions");
  container.innerHTML = '<p class="empty-state">加载中...</p>';
  try {
    const payload = await api("/api/submissions/mine");
    container.innerHTML = payload.submissions.length ? payload.submissions.map(submissionItem).join("") : '<p class="empty-state">还没有投稿。</p>';
  } catch (error) {
    container.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  }
}

function clearSubmissionImage() {
  state.uploadData = "";
  $("#submission-file").value = "";
  $("#submission-preview").removeAttribute("src");
  $("#submission-preview-wrap").classList.add("hidden");
}

function updateSubmissionOpenControls() {
  $("#submission-open-details").classList.toggle("hidden", !$("#submission-open").checked);
}

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

function canvasToBlob(canvas, type, quality) {
  return new Promise(resolve => canvas.toBlob(resolve, type, quality));
}

async function prepareSubmissionImage(blob) {
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(blob.type)) throw new Error("只支持 PNG、JPEG 或 WebP 图片");
  if (blob.size <= MAX_SUBMISSION_IMAGE_BYTES) return blob;
  const bitmap = await createImageBitmap(blob);
  const scale = Math.min(1, 1920 / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  const compressed = await canvasToBlob(canvas, "image/webp", 0.88);
  if (!compressed || compressed.size > MAX_SUBMISSION_IMAGE_BYTES) throw new Error("截图仍超过 3 MB，请截取更小的窗口或区域");
  return compressed;
}

async function setSubmissionImage(blob, source) {
  const prepared = await prepareSubmissionImage(blob);
  state.uploadData = await blobToDataUrl(prepared);
  $("#submission-preview").src = state.uploadData;
  $("#submission-image-source").textContent = source;
  $("#submission-preview-wrap").classList.remove("hidden");
}

async function previewSubmissionFile(event) {
  const file = event.target.files[0];
  clearSubmissionImage();
  if (!file) return;
  try {
    await setSubmissionImage(file, `本地图片 · ${file.name}`);
  } catch (error) {
    clearSubmissionImage();
    toast(error.message, "error");
  }
}

async function dropSubmissionImage(event) {
  event.preventDefault();
  $("#submission-drop-zone").classList.remove("dragging");
  const file = [...event.dataTransfer.files].find(item => item.type.startsWith("image/"));
  if (!file) return toast("请拖入 PNG、JPEG 或 WebP 图片", "error");
  clearSubmissionImage();
  try {
    await setSubmissionImage(file, `拖拽图片 · ${file.name}`);
    toast("图片已添加");
  } catch (error) {
    clearSubmissionImage();
    toast(error.message, "error");
  }
}

async function pasteSubmissionScreenshot(event) {
  const items = event?.clipboardData?.items || [];
  const imageItem = [...items].find(item => item.type.startsWith("image/"));
  if (!imageItem) return false;
  event.preventDefault();
  try {
    await setSubmissionImage(imageItem.getAsFile(), "粘贴的截图");
    $("#submission-file").value = "";
    toast("截图已粘贴");
  } catch (error) {
    toast(error.message, "error");
  }
  return true;
}

async function readSubmissionClipboard() {
  if (!navigator.clipboard?.read) return toast("请直接按 Ctrl/Cmd+V 粘贴截图", "error");
  try {
    const items = await navigator.clipboard.read();
    for (const item of items) {
      const type = item.types.find(value => value.startsWith("image/"));
      if (type) {
        await setSubmissionImage(await item.getType(type), "剪贴板截图");
        $("#submission-file").value = "";
        toast("截图已粘贴");
        return;
      }
    }
    toast("剪贴板里没有图片", "error");
  } catch (_) {
    toast("无法读取剪贴板，请在表单内按 Ctrl/Cmd+V", "error");
  }
}

async function submitClue(event) {
  event.preventDefault();
  const button = $("#submission-form button[type='submit']");
  setButtonBusy(button, true, "上传中...");
  try {
    await api("/api/submissions", {
      method: "POST",
      body: {
        answer: $("#submission-answer").value,
        clueKind: selectedValue("#submission-kind") || "image",
        imageData: state.uploadData,
        textClue: $("#submission-text").value,
        note: $("#submission-note").value,
        suggestedBrain: $("#submission-brain").checked,
        suggestedOpen: $("#submission-open").checked,
        acceptedAnswers: $("#submission-accepted-answers").value,
        verificationText: $("#submission-verification-text").value,
      },
    });
    toast("投稿已进入审核队列");
    $("#submission-form").reset();
    updateSubmissionOpenControls();
    clearSubmissionImage();
    selectSegment($("#submission-kind"), $("#submission-kind button[data-value='image']"));
    loadMySubmissions();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

function reviewItem(item) {
  const media = item.imageUrl
    ? `<div class="review-media"><img src="${escapeHtml(item.imageUrl)}" alt="待审核裁图"></div>`
    : `<div class="review-media"><p>${escapeHtml(item.textClue || "无图片")}</p></div>`;
  const today = new Date().toISOString().slice(0, 10);
  return `<article class="review-item" data-review-id="${item.id}">${media}<form class="review-form"><div class="full submission-copy"><strong>${escapeHtml(item.answer)} · ${escapeHtml(item.username)}</strong><span>${escapeHtml(item.clueKind)}${item.suggestedBrain ? " · 建议最强大脑" : ""}${item.suggestedOpen ? " · 建议开放题" : ""}</span>${item.note ? `<p>${escapeHtml(item.note)}</p>` : ""}</div><label>题名<input name="title" required></label><label>Rating<input name="rating" type="number" min="800" max="4000" step="100" required></label><label>Round<input name="roundNumber" type="number" min="1" required></label><label>组别<select name="division"><option>Div. 1</option><option selected>Div. 2</option><option>Div. 3</option><option>Div. 4</option><option>Edu</option><option>Div. 1 + Div. 2</option></select></label><label>比赛日期<input name="contestDate" type="date" value="${today}" required></label><label class="check-line"><input name="brain" type="checkbox" ${item.suggestedBrain ? "checked" : ""}> 最强大脑</label><label class="check-line"><input name="openMode" type="checkbox" ${item.suggestedOpen ? "checked" : ""}> 开放题</label><label class="full">其他可接受题号<input name="acceptedAnswers" value="${escapeHtml(item.acceptedAnswers || "")}" placeholder="123A, 456B"></label><label class="full">自动核验用题面原文<textarea name="verificationText" rows="3">${escapeHtml(item.verificationText || "")}</textarea></label><label class="full">审核备注<textarea name="reviewNote" rows="2"></textarea></label><div class="review-actions"><button class="danger-btn reject-review" type="button">不通过</button><button class="primary approve-review" type="submit">通过并入库</button></div></form></article>`;
}

async function loadAdminSubmissions() {
  const container = $("#review-list");
  if (!state.user?.isAdmin) {
    container.innerHTML = '<p class="empty-state">需要管理员权限。</p>';
    return;
  }
  container.innerHTML = '<p class="empty-state">加载中...</p>';
  try {
    const payload = await api("/api/admin/submissions");
    const pending = payload.submissions.filter(item => item.status === "pending");
    container.innerHTML = pending.length ? pending.map(reviewItem).join("") : '<p class="empty-state">暂无待审核投稿。</p>';
    $$(".review-item", container).forEach(item => {
      const form = $("form", item);
      form.addEventListener("submit", event => { event.preventDefault(); reviewSubmission(item, "approve"); });
      $(".reject-review", item).addEventListener("click", () => reviewSubmission(item, "reject"));
    });
  } catch (error) {
    container.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  }
}

function openCandidateItem(item) {
  return `<article class="review-item candidate-item" data-candidate-id="${item.id}"><div class="submission-copy"><strong>${escapeHtml(item.answer)} → ${escapeHtml(item.questionTitle)}</strong><span>${escapeHtml(item.questionKey)} · ${escapeHtml(item.username)} · 被提交 ${item.hitCount} 次</span><p>自动匹配度 ${(item.similarity * 100).toFixed(1)}%</p></div><form class="candidate-actions"><label>Round（可选）<input name="roundNumber" type="number" min="0" value="0"></label><label>组别<select name="division"><option selected>Open</option><option>Div. 1</option><option>Div. 2</option><option>Div. 3</option><option>Div. 4</option><option>Edu</option><option>Div. 1 + Div. 2</option></select></label><button class="danger-btn reject-candidate" type="button">驳回</button><button class="primary approve-candidate" type="submit">加入答案</button></form></article>`;
}

async function loadOpenCandidates() {
  const container = $("#open-candidate-list");
  if (!state.user?.isAdmin) return;
  container.innerHTML = '<p class="empty-state">加载中...</p>';
  try {
    const payload = await api("/api/admin/open-candidates");
    const pending = payload.candidates.filter(item => item.status === "pending");
    container.innerHTML = pending.length ? pending.map(openCandidateItem).join("") : '<p class="empty-state">暂无待审核候选。</p>';
    $$(".candidate-item", container).forEach(item => {
      $("form", item).addEventListener("submit", event => { event.preventDefault(); reviewOpenCandidate(item, "approve"); });
      $(".reject-candidate", item).addEventListener("click", () => reviewOpenCandidate(item, "reject"));
    });
  } catch (error) {
    container.innerHTML = `<p class="empty-state">${escapeHtml(error.message)}</p>`;
  }
}

async function reviewOpenCandidate(item, action) {
  const form = $("form", item);
  const fields = new FormData(form);
  const button = action === "approve" ? $(".approve-candidate", item) : $(".reject-candidate", item);
  setButtonBusy(button, true, "处理中...");
  try {
    await api(`/api/admin/open-candidates/${item.dataset.candidateId}/review`, {
      method: "POST",
      body: { action, roundNumber: fields.get("roundNumber"), division: fields.get("division") },
    });
    toast(action === "approve" ? "候选已加入答案集合" : "候选已驳回");
    loadOpenCandidates();
  } catch (error) {
    toast(error.message, "error");
    setButtonBusy(button, false);
  }
}

function permissionRole(user) {
  if (user.isSuperAdmin) return "超级管理员";
  if (user.isAdmin) return "审核管理员";
  return "普通用户";
}

async function loadPermissionUsers() {
  const body = $("#permissions-body");
  if (!state.user?.isSuperAdmin) {
    body.innerHTML = '<tr><td colspan="4">需要超级管理员权限。</td></tr>';
    return;
  }
  body.innerHTML = '<tr><td colspan="4">加载中...</td></tr>';
  try {
    const payload = await api("/api/admin/users");
    body.innerHTML = payload.users.map(user => {
      const action = user.isSuperAdmin
        ? '<span class="locked-role">仅服务器可修改</span>'
        : `<button class="${user.isAdmin ? "danger-btn" : "secondary"} role-action" type="button" data-user-id="${user.id}" data-next-admin="${user.isAdmin ? "false" : "true"}">${user.isAdmin ? "撤销管理员" : "设为管理员"}</button>`;
      return `<tr><td><strong>${escapeHtml(user.username)}</strong></td><td>${permissionRole(user)}</td><td>${new Date(user.createdAt * 1000).toLocaleDateString("zh-CN")}</td><td>${action}</td></tr>`;
    }).join("");
    $$(".role-action", body).forEach(button => button.addEventListener("click", () => changeAdminRole(button)));
  } catch (error) {
    body.innerHTML = `<tr><td colspan="4">${escapeHtml(error.message)}</td></tr>`;
  }
}

async function changeAdminRole(button) {
  setButtonBusy(button, true);
  try {
    await api(`/api/admin/users/${button.dataset.userId}/role`, {
      method: "POST",
      body: { isAdmin: button.dataset.nextAdmin === "true" },
    });
    toast(button.dataset.nextAdmin === "true" ? "已授予审核管理员权限" : "已撤销审核管理员权限");
    loadPermissionUsers();
  } catch (error) {
    toast(error.message, "error");
    setButtonBusy(button, false);
  }
}

async function reviewSubmission(item, action) {
  const form = $("form", item);
  const button = action === "approve" ? $(".approve-review", item) : $(".reject-review", item);
  const fields = new FormData(form);
  if (action === "approve" && !form.reportValidity()) return;
  setButtonBusy(button, true, "处理中...");
  try {
    const dateValue = fields.get("contestDate");
    await api(`/api/admin/submissions/${item.dataset.reviewId}/review`, {
      method: "POST",
      body: {
        action,
        title: fields.get("title"),
        rating: fields.get("rating"),
        roundNumber: fields.get("roundNumber"),
        division: fields.get("division"),
        contestTime: dateValue ? Math.floor(new Date(`${dateValue}T00:00:00Z`).getTime() / 1000) : 0,
        brain: fields.get("brain") === "on",
        openMode: fields.get("openMode") === "on",
        acceptedAnswers: fields.get("acceptedAnswers"),
        verificationText: fields.get("verificationText"),
        reviewNote: fields.get("reviewNote"),
      },
    });
    toast(action === "approve" ? "已通过并加入题库" : "已标记为不通过");
    loadAdminSubmissions();
  } catch (error) {
    toast(error.message, "error");
    setButtonBusy(button, false);
  }
}

async function submitSoloAnswer(event) {
  event.preventDefault();
  const button = $("#solo-answer-form .submit-answer");
  let cooldown = 0;
  setButtonBusy(button, true, "判定中...");
  try {
    const payload = await api("/api/quiz/answer", {
      method: "POST",
      body: { token: state.solo.question.token, ...answerPayload("solo", state.solo.answerMode) },
    });
    if (!payload.settled) {
      if (payload.pendingReview) {
        $("#solo-answer-form").reset();
        renderDivisionChoices("solo", state.solo.question);
        setSoloAnswerMode("contest");
        toast("这个答案尚未收录，已进入管理员审核；本次不扣机会");
        cooldown = payload.retryAfter;
        return;
      }
      state.solo.attemptsUsed = payload.attemptsUsed;
      $("#solo-attempts-status").textContent = `${payload.attemptsUsed} / ${state.solo.maxAttempts} 次`;
      $("#solo-answer-form").reset();
      renderDivisionChoices("solo", state.solo.question);
      setSoloAnswerMode("contest");
      toast(`未命中，剩余 ${payload.attemptsLeft} 次尝试`);
      cooldown = payload.retryAfter;
      return;
    }
    renderSoloResolution(payload, false);
  } catch (error) {
    if (error.status === 429) cooldown = error.payload?.retryAfter || 5;
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
    if (cooldown) startSoloRetryCooldown(cooldown);
  }
}

function startSoloRetryCooldown(seconds) {
  clearInterval(state.solo.retryTimer);
  const button = $("#solo-answer-form .submit-answer");
  let remaining = Math.max(1, Number(seconds) || 5);
  button.disabled = true;
  button.textContent = `${remaining}s 后重试`;
  state.solo.retryTimer = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0 || state.solo.resolved) {
      clearInterval(state.solo.retryTimer);
      state.solo.retryTimer = null;
      button.disabled = false;
      button.textContent = "提交答案";
      return;
    }
    button.textContent = `${remaining}s 后重试`;
  }, 1000);
}

function ratingDeltaText(payload) {
  if (!payload.rated) return "";
  const sign = payload.ratingDelta > 0 ? "+" : "";
  return ` · Rating ${sign}${payload.ratingDelta}`;
}

function renderSoloResolution(payload, abandoned) {
  clearInterval(state.solo.timer);
  clearInterval(state.solo.retryTimer);
  state.solo.resolved = true;
  state.solo.settling = false;
  state.solo.attemptsUsed = payload.attemptsUsed;
  if (state.solo.timed) $("#solo-attempts-status").textContent = `${payload.attemptsUsed} / ${state.solo.maxAttempts} 次`;
  state.user = payload.user;
  renderUser();
  $("#solo-answer-form").classList.add("hidden");
  $("#abandon-solo").classList.add("hidden");
  const result = $("#solo-result");
  const distanceScoring = payload.scoringMode === "distance";
  result.className = `result-strip ${payload.correct || distanceScoring && payload.points > 0 ? "" : "wrong"}`;
  const headings = { timeout: "倒计时结束", attempts: "尝试次数已用完", abandoned: "已放弃此题" };
  const heading = abandoned
    ? "已放弃此题"
    : payload.solutionWithheld
      ? "未完全命中，结果不公开"
    : distanceScoring
      ? `本题 ${payload.points} 分 · 距离 ${payload.distance}`
      : payload.correct ? `回答正确，+${payload.points} 分` : (headings[payload.terminalReason] || "没有命中");
  const detail = payload.solutionWithheld
    ? "<span>最强大脑题未命中，正确答案暂不公开。</span>"
    : `<span>${escapeHtml(payload.solution.title)} · ${payload.solution.rating}</span><br><span>${solutionText(payload.solution)}</span><br><a href="${escapeHtml(payload.solution.sourceUrl)}" target="_blank" rel="noreferrer">查看原题</a>`;
  result.innerHTML = `<strong>${heading}${ratingDeltaText(payload)}</strong>${detail} <button id="next-solo" class="text-btn" type="button">下一题</button>`;
  $("#next-solo").addEventListener("click", nextSoloQuestion);
}

async function abandonSoloQuestion() {
  if (!state.solo.question || state.solo.resolved) return;
  const button = $("#abandon-solo");
  state.solo.settling = true;
  setButtonBusy(button, true, "结算中...");
  try {
    const payload = await api("/api/quiz/abandon", {
      method: "POST",
      body: { token: state.solo.question.token },
    });
    renderSoloResolution(payload, true);
  } catch (error) {
    state.solo.settling = false;
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

async function timeoutSoloQuestion() {
  if (!state.solo.question || state.solo.resolved || state.solo.settling) return;
  state.solo.settling = true;
  $("#solo-answer-form .submit-answer").disabled = true;
  $("#abandon-solo").disabled = true;
  try {
    const payload = await api("/api/quiz/timeout", {
      method: "POST",
      body: { token: state.solo.question.token },
    });
    renderSoloResolution(payload, false);
  } catch (error) {
    state.solo.settling = false;
    $("#solo-answer-form .submit-answer").disabled = false;
    $("#abandon-solo").disabled = false;
    toast(error.message, "error");
  }
}

function resetSoloSession() {
  clearInterval(state.solo.timer);
  clearInterval(state.solo.retryTimer);
  state.solo.question = null;
  state.solo.resolved = true;
  state.solo.settling = false;
  $("#solo-game").classList.add("hidden");
  $("#solo-setup").classList.remove("hidden");
}

async function endSoloSession() {
  const button = $("#end-solo");
  if (state.solo.question && !state.solo.resolved) {
    if (!window.confirm("当前题会按放弃结算，确定结束本次吗？")) return;
    state.solo.settling = true;
    setButtonBusy(button, true, "结算中...");
    try {
      const payload = await api("/api/quiz/abandon", {
        method: "POST",
        body: { token: state.solo.question.token },
      });
      state.user = payload.user;
      renderUser();
      resetSoloSession();
      toast(`本次已结束${ratingDeltaText(payload)}`);
    } catch (error) {
      state.solo.settling = false;
      toast(error.message, "error");
    } finally {
      setButtonBusy(button, false);
    }
    return;
  }
  resetSoloSession();
  toast("本次已结束");
}

function formatDurationMs(elapsedMs) {
  const totalSeconds = Math.max(0, Math.floor(Number(elapsedMs || 0) / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function renderDailyBoard(players = []) {
  const body = $("#daily-board-body");
  body.innerHTML = players.length ? players.map(player => `<tr><td class="rank">#${player.rank}</td><td><strong>${escapeHtml(player.username)}</strong></td><td>${player.score.toLocaleString()}</td><td>${formatDurationMs(player.elapsedMs)}</td></tr>`).join("") : '<tr><td colspan="4">今天还没有完成挑战的玩家</td></tr>';
}

function stopDailyTimer() {
  clearInterval(state.daily.timer);
  state.daily.timer = null;
}

function updateDailyTimer() {
  const elapsed = Math.floor((Date.now() - state.daily.startedAt) / 1000);
  const left = Math.max(0, state.daily.secondsLeft - elapsed);
  $("#daily-timer").textContent = `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
  if (!left && !state.daily.settling) timeoutDaily();
}

function renderDaily(challenge) {
  state.daily.challenge = challenge;
  stopDailyTimer();
  $("#daily-total").textContent = challenge.totalScore.toLocaleString();
  const ready = $("#daily-ready");
  const game = $("#daily-game");
  if (challenge.status !== "playing") {
    ready.classList.remove("hidden");
    game.classList.add("hidden");
    const button = $("#daily-start");
    if (challenge.status === "finished") {
      $("#daily-copy").textContent = `今日挑战已完成，总分 ${challenge.totalScore.toLocaleString()}。`;
      button.classList.add("hidden");
      renderDailyBoard(challenge.leaderboard || []);
    } else {
      $("#daily-copy").textContent = "所有玩家面对同一组五道题，每题限时 120 秒，每题最高 5000 分。";
      button.classList.remove("hidden");
      button.textContent = challenge.status === "between" ? `继续第 ${challenge.position + 1} 题` : "开始今日挑战";
    }
    return;
  }
  ready.classList.add("hidden");
  game.classList.remove("hidden");
  const question = challenge.question;
  $("#daily-round-label").textContent = `第 ${question.position} / ${challenge.questionCount} 题`;
  $("#daily-progress").style.width = `${100 * question.position / challenge.questionCount}%`;
  $("#daily-clue").src = `${question.clueUrl}&v=${Date.now()}`;
  $("#daily-answer-form").reset();
  $("#daily-answer-form").classList.remove("hidden");
  $("#daily-answer-form .submit-answer").disabled = false;
  $("#daily-result").className = "result-strip hidden";
  state.daily.secondsLeft = question.secondsLeft;
  state.daily.startedAt = Date.now();
  state.daily.settling = false;
  state.daily.timer = setInterval(updateDailyTimer, 250);
  updateDailyTimer();
  $("#daily-contest-answer").focus();
}

async function loadDaily() {
  try {
    const payload = await api("/api/daily");
    renderDaily(payload.challenge);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function loadDailyLeaderboard() {
  try {
    const payload = await api("/api/daily/leaderboard");
    renderDailyBoard(payload.players);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function startDaily() {
  const button = $("#daily-start");
  setButtonBusy(button, true, "正在抽题...");
  try {
    const payload = await api("/api/daily/start", { method: "POST", body: {} });
    renderDaily(payload.challenge);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

function renderDailyResolution(payload) {
  stopDailyTimer();
  state.daily.settling = false;
  state.user = payload.user;
  state.daily.challenge = payload.challenge;
  renderUser();
  $("#daily-total").textContent = payload.challenge.totalScore.toLocaleString();
  $("#daily-answer-form").classList.add("hidden");
  const result = $("#daily-result");
  result.className = `result-strip${payload.score ? "" : " wrong"}`;
  const scoreLine = payload.timedOut ? "本题超时，0 分" : `本题 ${payload.score.toLocaleString()} 分 · 距离 ${payload.distance}`;
  const gaps = payload.timedOut ? "" : `<span>Contest 差 ${payload.contestGap}，题号差 ${payload.indexGap}${payload.exact ? "，完全命中" : ""}</span><br>`;
  const nextLabel = payload.completed ? "查看今日榜" : "下一题";
  result.innerHTML = `<strong>${scoreLine}</strong>${gaps}<span>${escapeHtml(payload.solution.title)} · ${solutionText(payload.solution)}</span> <button id="daily-next" class="text-btn" type="button">${nextLabel}</button>`;
  $("#daily-next").addEventListener("click", () => {
    if (payload.completed) renderDaily(payload.challenge);
    else startDaily();
  });
}

async function submitDailyAnswer(event) {
  event.preventDefault();
  const button = $("#daily-answer-form .submit-answer");
  state.daily.settling = true;
  setButtonBusy(button, true, "计分中...");
  try {
    const payload = await api("/api/daily/answer", {
      method: "POST",
      body: { contestAnswer: $("#daily-contest-answer").value },
    });
    renderDailyResolution(payload.result);
  } catch (error) {
    state.daily.settling = false;
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

async function timeoutDaily() {
  if (state.daily.settling) return;
  state.daily.settling = true;
  $("#daily-answer-form .submit-answer").disabled = true;
  try {
    const payload = await api("/api/daily/timeout", { method: "POST", body: {} });
    renderDailyResolution(payload.result);
  } catch (error) {
    state.daily.settling = false;
    if (error.status === 409 && error.payload?.secondsLeft) {
      state.daily.secondsLeft = error.payload.secondsLeft;
      state.daily.startedAt = Date.now();
      return;
    }
    toast(error.message, "error");
  }
}

async function loadLeaderboard() {
  const body = $("#leaderboard-body");
  body.innerHTML = '<tr><td colspan="6">加载中...</td></tr>';
  try {
    const payload = await api("/api/leaderboard");
    body.innerHTML = payload.players.length ? payload.players.map((p, index) => `<tr><td class="rank">#${index + 1}</td><td><strong>${escapeHtml(p.username)}</strong></td><td>${p.rating}</td><td>${p.games}</td><td>${p.wins}</td><td>${p.totalScore.toLocaleString()}</td></tr>`).join("") : '<tr><td colspan="6">还没有玩家</td></tr>';
  } catch (error) {
    body.innerHTML = `<tr><td colspan="6">${escapeHtml(error.message)}</td></tr>`;
  }
}

async function createRoom() {
  const button = $("#create-room");
  setButtonBusy(button, true, "创建中...");
  try {
    const payload = await api("/api/matches", {
      method: "POST",
      body: {
        rated: selectedValue("#battle-mode") === "true",
        scoringMode: selectedValue("#battle-scoring-mode") || "classic",
        rounds: Number($("#battle-rounds").value),
        roundSeconds: Number($("#battle-round-seconds").value),
        abandonSeconds: Number($("#battle-abandon-seconds").value),
        penaltyEnabled: selectedValue("#battle-penalty-mode") === "true",
        penaltyFirst: Number($("#battle-penalty-first").value),
        penaltySecond: Number($("#battle-penalty-second").value),
        penaltyRepeat: Number($("#battle-penalty-repeat").value),
        filters: {
          difficulty: selectedValue("#battle-difficulty") || "medium",
          questionMode: selectedValue("#battle-question-mode") || "standard",
        },
      },
    });
    enterRoom(payload.code);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

async function joinRoom() {
  const button = $("#join-room");
  const code = $("#join-code").value.trim().toUpperCase();
  if (code.length !== 6) return toast("请输入 6 位房间码", "error");
  setButtonBusy(button, true, "加入中...");
  try {
    const payload = await api("/api/matches/join", { method: "POST", body: { code } });
    enterRoom(payload.code);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

function enterRoom(code) {
  state.battle.code = code;
  state.battle.roundSeen = 0;
  $("#battle-setup").classList.add("hidden");
  $("#battle-room").classList.remove("hidden");
  $("#room-code").textContent = code;
  startBattlePoll();
  pollBattle();
}

function startBattlePoll() {
  if (!state.battle.code || state.battle.poll) return;
  state.battle.poll = setInterval(pollBattle, 1000);
}

function stopBattlePoll() {
  clearInterval(state.battle.poll);
  state.battle.poll = null;
}

async function pollBattle() {
  if (!state.battle.code || state.battle.polling) return;
  state.battle.polling = true;
  try {
    const payload = await api(`/api/matches/${state.battle.code}`);
    renderBattle(payload.match);
  } catch (error) {
    if (error.status === 401) stopBattlePoll();
    if (error.status === 403 || error.status === 404) resetBattleRoom();
    toast(error.message, "error");
  } finally {
    state.battle.polling = false;
  }
}

function renderPlayers(players) {
  const [host, guest] = players;
  $("#battle-players").innerHTML = `<div class="player-chip"><strong>${escapeHtml(host.username)}</strong><span>${host.rating} rating</span></div><div class="versus-mark">VS</div>${guest ? `<div class="player-chip"><strong>${escapeHtml(guest.username)}</strong><span>${guest.rating} rating</span></div>` : '<div class="player-chip empty"><strong>等待加入</strong><span>分享房间码</span></div>'}`;
}

function renderScoreboard(players) {
  $("#battle-scoreboard").innerHTML = players.filter(Boolean).map(p => `<div class="${p.you ? "you" : ""}"><strong>${escapeHtml(p.username)}${p.you ? "（你）" : ""}</strong><span>${p.score}</span></div>`).join("");
}

function setBattleAnswerMode(mode) {
  state.battle.answerMode = mode;
  $$('[data-battle-answer-mode]').forEach(btn => btn.classList.toggle("active", btn.dataset.battleAnswerMode === mode));
  $("#battle-contest-fields").classList.toggle("hidden", mode !== "contest");
  $("#battle-round-fields").classList.toggle("hidden", mode !== "round");
}

function renderBattle(match) {
  state.battle.current = match;
  const questionMode = match.questionMode === "open" ? "开放多解" : "普通题";
  $("#room-mode").textContent = `${questionMode} · ${match.rated ? "Rating 模式" : "娱乐模式"} · ${match.scoringMode === "distance" ? "积分赛" : "抢答赛"}`;
  const penalties = match.rules.penalties;
  $("#battle-rules-summary").innerHTML = [
    `<span>每题 ${match.rules.roundSeconds} 秒</span>`,
    `<span>${match.scoringMode === "distance" ? "单次锁定 · 每题最高 5000 分" : match.rules.penaltyEnabled ? `错答罚时 ${penalties[0]} / ${penalties[1]} / ${penalties[2]} 秒` : "错答不罚时"}</span>`,
    `<span>一方放弃后最多等待 ${match.rules.abandonSeconds} 秒</span>`,
  ].join("");
  if (match.status === "waiting") {
    $("#lobby-state").classList.remove("hidden");
    $("#battle-game").classList.add("hidden");
    renderPlayers(match.players);
    const ready = Boolean(match.players[1]);
    $("#lobby-message").textContent = ready ? "两位玩家已就位" : "等待另一位玩家加入...";
    $("#start-battle").classList.toggle("hidden", !(ready && match.isHost));
    return;
  }

  $("#lobby-state").classList.add("hidden");
  $("#battle-game").classList.remove("hidden");
  renderScoreboard(match.players);
  $("#battle-round-label").textContent = `第 ${match.round} / ${match.rounds} 题${match.openMode ? " · 开放题" : ""}`;
  $("#battle-progress").style.width = `${100 * match.round / match.rounds}%`;
  $("#battle-timer").textContent = `${match.secondsLeft}s`;

  if (match.status === "finished") {
    stopBattlePoll();
    $("#battle-answer-form").classList.add("hidden");
    const result = $("#battle-result");
    result.className = "result-strip";
    result.innerHTML = `<strong>对战结束</strong><span>${match.winner === "draw" ? "平局" : `${escapeHtml(match.winner)} 获胜`}</span>`;
    refreshMe();
    return;
  }

  const clue = $("#battle-clue");
  const cluePath = new URL(match.clueUrl, location.origin).pathname + new URL(match.clueUrl, location.origin).search;
  if (!clue.src.endsWith(cluePath)) clue.src = match.clueUrl;
  if (state.battle.roundSeen !== match.round) {
    state.battle.roundSeen = match.round;
    $("#battle-answer-form").reset();
    setBattleAnswerMode("contest");
    renderDivisionChoices("battle", match);
  }
  $("#battle-answer-mode").classList.toggle("hidden", match.scoringMode === "distance");

  if (match.phase === "playing") {
    const form = $("#battle-answer-form");
    const result = $("#battle-result");
    const submit = $("#battle-answer-form .submit-answer");
    const abandon = $("#abandon-battle");
    form.classList.toggle("hidden", match.ownStatus !== "playing");
    abandon.disabled = match.ownStatus !== "playing";
    submit.dataset.original = match.scoringMode === "distance" ? "锁定答案" : "提交答案";
    submit.disabled = !match.canSubmit;
    submit.textContent = match.cooldownLeft > 0 ? `${match.cooldownLeft} 秒后可提交` : submit.dataset.original;
    result.className = "result-strip hidden";
    if (match.ownStatus === "correct") {
      result.className = "result-strip";
      result.innerHTML = `<strong>${match.scoringMode === "distance" ? "答案已锁定" : "回答正确"}</strong><span>等待对手完成本题。</span>`;
    } else if (match.ownStatus === "scored") {
      result.className = "result-strip";
      result.innerHTML = "<strong>答案已锁定</strong><span>等待对手完成本题。</span>";
    } else if (match.ownStatus === "abandoned") {
      result.className = "result-strip wrong";
      result.innerHTML = "<strong>你已放弃本题</strong><span>等待对手完成或短倒计时结束。</span>";
    } else if (match.attempts || match.opponentAbandoned) {
      const messages = [];
      if (match.attempts) messages.push(`已尝试 ${match.attempts} 次`);
      if (match.cooldownLeft > 0) messages.push(`错答罚时还剩 ${match.cooldownLeft} 秒`);
      if (match.opponentAbandoned) messages.push(`对手已放弃，你还有 ${match.secondsLeft} 秒`);
      result.className = `result-strip${match.cooldownLeft > 0 ? " wrong" : ""}`;
      result.innerHTML = `<strong>${match.cooldownLeft > 0 ? "暂时不能提交" : "可以继续作答"}</strong><span>${messages.join(" · ")}</span>`;
    }
  } else if (match.phase === "reveal") {
    $("#battle-answer-form").classList.add("hidden");
    const result = $("#battle-result");
    const labels = { correct: "完全命中", scored: "已计分", abandoned: "放弃", pending: "候选待审核", miss: "未命中", timeout: "未作答" };
    const rows = match.reveal.answers.map(a => {
      const attempts = a.attempts ? `，尝试 ${a.attempts} 次` : "";
      const points = ["correct", "scored"].includes(a.status) ? ` +${a.points}` : "";
      const distance = a.distance !== null && a.distance !== undefined ? `，距离 ${a.distance}` : "";
      const answer = a.answer ? `（${escapeHtml(a.answer)}）` : "";
      return `${escapeHtml(a.username)}：${labels[a.status] || "未作答"}${answer}${points}${distance}${attempts}`;
    }).join(" · ") || "本轮无人作答";
    result.className = "result-strip";
    if (match.reveal.solutionWithheld) {
      result.innerHTML = `<strong>最强大脑题不公开答案</strong><span>本题继续留在题库中。</span><br><span>${rows}</span>`;
    } else {
      result.innerHTML = `<strong>${escapeHtml(match.reveal.solution.title)} · ${match.reveal.solution.rating}</strong><span>${solutionText(match.reveal.solution)}</span><br><span>${rows}</span>`;
    }
  }
}

async function startBattle() {
  const button = $("#start-battle");
  setButtonBusy(button, true, "启动中...");
  try {
    await api(`/api/matches/${state.battle.code}/start`, { method: "POST", body: {} });
    await pollBattle();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

async function submitBattleAnswer(event) {
  event.preventDefault();
  const button = $("#battle-answer-form .submit-answer");
  setButtonBusy(button, true, "判定中...");
  let refresh = false;
  try {
    const payload = await api(`/api/matches/${state.battle.code}/answer`, {
      method: "POST",
      body: answerPayload("battle", state.battle.answerMode),
    });
    const wrongMessage = payload.pendingReview
      ? "这个答案尚未收录，已送管理员审核；本次不计错答"
      : payload.cooldown ? `没有命中，罚时 ${payload.cooldown} 秒` : "没有命中，可以继续尝试";
    const scored = state.battle.current?.scoringMode === "distance" && payload.settled;
    toast(scored ? `答案已锁定，+${payload.points} 分` : payload.correct ? `回答正确，+${payload.points} 分` : wrongMessage, payload.correct || payload.pendingReview || scored ? "" : "error");
    refresh = true;
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
  if (refresh) await pollBattle();
}

async function abandonBattleQuestion() {
  const match = state.battle.current;
  if (!match || match.phase !== "playing" || match.ownStatus !== "playing") return;
  if (!window.confirm("放弃后本题不能继续作答，确定放弃吗？")) return;
  const button = $("#abandon-battle");
  setButtonBusy(button, true, "放弃中...");
  try {
    await api(`/api/matches/${state.battle.code}/abandon`, { method: "POST", body: {} });
    toast("已放弃本题");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
  await pollBattle();
}

function updateBattlePenaltyControls() {
  const enabled = selectedValue("#battle-penalty-mode") === "true";
  $("#battle-penalty-settings").classList.toggle("disabled", !enabled);
  $$("#battle-penalty-settings input").forEach(input => { input.disabled = !enabled; });
}

function updateBattleScoringControls() {
  const distance = selectedValue("#battle-scoring-mode") === "distance";
  $("#battle-scoring-hint").classList.toggle("hidden", !distance);
  $("#battle-penalty-block").classList.toggle("hidden", distance);
}

function resetBattleRoom() {
  stopBattlePoll();
  state.battle.code = null;
  state.battle.roundSeen = 0;
  state.battle.current = null;
  $("#battle-room").classList.add("hidden");
  $("#battle-setup").classList.remove("hidden");
}

async function leaveRoom() {
  const button = $("#exit-room");
  const code = state.battle.code;
  if (!code) return resetBattleRoom();
  setButtonBusy(button, true, "退出中...");
  try {
    const payload = await api(`/api/matches/${code}/leave`, { method: "POST", body: {} });
    resetBattleRoom();
    toast(payload.forfeited ? "已退出，本场按弃权结算" : "已退出房间");
  } catch (error) {
    if (error.status === 403 || error.status === 404) resetBattleRoom();
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

async function refreshMe() {
  try {
    const payload = await api("/api/me");
    if (payload.user) {
      state.user = payload.user;
      state.csrf = payload.csrf;
      renderUser();
    }
  } catch (_) { /* next request will surface auth errors */ }
}

function bindEvents() {
  $$('[data-auth-mode]').forEach(btn => btn.addEventListener("click", () => setAuthMode(btn.dataset.authMode)));
  $("#auth-form").addEventListener("submit", submitAuth);
  $("#logout-btn").addEventListener("click", logout);
  $("#menu-btn").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
  $$(".nav-item").forEach(btn => btn.addEventListener("click", () => showView(btn.dataset.view)));
  $$('[data-go]').forEach(btn => btn.addEventListener("click", () => showView(btn.dataset.go)));
  $$("#battle-mode button, #submission-kind button").forEach(btn => btn.addEventListener("click", () => selectSegment(btn.parentElement, btn)));
  $$("#solo-difficulty button, #battle-difficulty button").forEach(btn => btn.addEventListener("click", () => { selectSegment(btn.parentElement, btn); updateScoringCompatibility(); }));
  $$("#solo-question-mode button, #battle-question-mode button").forEach(btn => btn.addEventListener("click", () => { selectSegment(btn.parentElement, btn); updateQuestionModeHints(); }));
  $$("#solo-scoring-mode button").forEach(btn => btn.addEventListener("click", () => { selectSegment(btn.parentElement, btn); updateSoloScoringControls(); }));
  $$("#battle-scoring-mode button").forEach(btn => btn.addEventListener("click", () => { selectSegment(btn.parentElement, btn); updateBattleScoringControls(); }));
  $$("#battle-penalty-mode button").forEach(btn => btn.addEventListener("click", () => { selectSegment(btn.parentElement, btn); updateBattlePenaltyControls(); }));
  $$("#solo-mode button").forEach(btn => btn.addEventListener("click", () => { selectSegment(btn.parentElement, btn); updateSoloModeControls(); }));
  $$("#solo-timing button").forEach(btn => btn.addEventListener("click", () => { selectSegment(btn.parentElement, btn); updateSoloTimingControls(); }));
  $("#solo-start").addEventListener("click", startSolo);
  $("#abandon-solo").addEventListener("click", abandonSoloQuestion);
  $("#end-solo").addEventListener("click", endSoloSession);
  $$('[data-answer-mode]').forEach(btn => btn.addEventListener("click", () => setSoloAnswerMode(btn.dataset.answerMode)));
  $("#solo-answer-form").addEventListener("submit", submitSoloAnswer);
  $("#refresh-board").addEventListener("click", loadLeaderboard);
  $("#daily-start").addEventListener("click", startDaily);
  $("#daily-answer-form").addEventListener("submit", submitDailyAnswer);
  $("#refresh-daily-board").addEventListener("click", loadDailyLeaderboard);
  $("#submission-file").addEventListener("change", previewSubmissionFile);
  $("#paste-screenshot").addEventListener("click", readSubmissionClipboard);
  $("#submission-drop-zone").addEventListener("dragover", event => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    event.currentTarget.classList.add("dragging");
  });
  $("#submission-drop-zone").addEventListener("dragleave", event => event.currentTarget.classList.remove("dragging"));
  $("#submission-drop-zone").addEventListener("drop", dropSubmissionImage);
  $("#remove-submission-image").addEventListener("click", clearSubmissionImage);
  $("#submission-form").addEventListener("paste", pasteSubmissionScreenshot);
  $("#submission-form").addEventListener("submit", submitClue);
  $("#submission-open").addEventListener("change", updateSubmissionOpenControls);
  $("#refresh-submissions").addEventListener("click", loadMySubmissions);
  $("#refresh-reviews").addEventListener("click", () => { loadAdminSubmissions(); loadOpenCandidates(); });
  $("#refresh-open-candidates").addEventListener("click", loadOpenCandidates);
  $("#refresh-permissions").addEventListener("click", loadPermissionUsers);
  $("#create-room").addEventListener("click", createRoom);
  $("#join-room").addEventListener("click", joinRoom);
  $("#join-code").addEventListener("input", event => { event.target.value = event.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ""); });
  $("#room-code").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(state.battle.code); toast("房间码已复制"); } catch (_) { toast("房间码复制失败", "error"); }
  });
  $("#start-battle").addEventListener("click", startBattle);
  $("#exit-room").addEventListener("click", leaveRoom);
  $("#abandon-battle").addEventListener("click", abandonBattleQuestion);
  $$('[data-battle-answer-mode]').forEach(btn => btn.addEventListener("click", () => setBattleAnswerMode(btn.dataset.battleAnswerMode)));
  $("#battle-answer-form").addEventListener("submit", submitBattleAnswer);
}

async function boot() {
  bindEvents();
  try {
    const payload = await api("/api/me");
    if (payload.user) {
      state.user = payload.user;
      state.csrf = payload.csrf;
      showAuthenticated(true);
      renderUser();
      showView("dashboard");
    } else {
      showAuthenticated(false);
    }
  } catch (error) {
    showAuthenticated(false);
    toast("无法连接服务器", "error");
  }
}

boot();
