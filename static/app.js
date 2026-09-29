const state = {
  user: null,
  csrf: "",
  authMode: "login",
  view: "dashboard",
  solo: { filters: null, question: null, answerMode: "contest", round: 0, startedAt: 0, timer: null },
  battle: { code: null, answerMode: "contest", poll: null, polling: false, roundSeen: 0 },
  uploadData: "",
};

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
  dashboard: ["OVERVIEW", "今天也来认几道题"],
  solo: ["SOLO QUIZ", "单人图寻"],
  battle: ["VERSUS", "双人对战"],
  submit: ["CONTRIBUTE", "投稿线索"],
  admin: ["MODERATION", "审核投稿"],
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
  if (name === "submit") loadMySubmissions();
  if (name === "admin") loadAdminSubmissions();
  if (name !== "battle" && state.battle.poll) stopBattlePoll();
  if (name === "battle" && state.battle.code) startBattlePoll();
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
    contestMin: $("#solo-contest-min").value,
    contestMax: $("#solo-contest-max").value,
    yearMin: $("#solo-year-min").value,
    yearMax: $("#solo-year-max").value,
    roundTypes: $$(".round-filter input:checked").map(el => el.value),
  };
}

async function startSolo() {
  state.solo.filters = soloFilters();
  state.solo.round = 0;
  $("#solo-setup").classList.add("hidden");
  $("#solo-game").classList.remove("hidden");
  await nextSoloQuestion();
}

async function nextSoloQuestion() {
  const button = $("#solo-start");
  try {
    setButtonBusy(button, true, "正在抽题...");
    const payload = await api("/api/quiz/next", { method: "POST", body: { filters: state.solo.filters } });
    state.solo.question = payload.question;
    state.solo.round += 1;
    state.solo.startedAt = Date.now();
    $("#solo-round-label").textContent = `第 ${state.solo.round} 题`;
    $("#solo-progress").style.width = `${Math.min(100, (state.solo.round % 10 || 10) * 10)}%`;
    $("#solo-clue").src = `${payload.question.clueUrl}?v=${Date.now()}`;
    $("#solo-result").className = "result-strip hidden";
    $("#solo-result").innerHTML = "";
    $("#solo-answer-form").classList.remove("hidden");
    $("#solo-answer-form").reset();
    renderDivisionChoices("solo", payload.question);
    setSoloAnswerMode("contest");
    $("#solo-contest-answer").focus();
    clearInterval(state.solo.timer);
    state.solo.timer = setInterval(updateSoloTimer, 250);
    updateSoloTimer();
  } catch (error) {
    $("#solo-setup").classList.remove("hidden");
    $("#solo-game").classList.add("hidden");
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

function updateSoloTimer() {
  const elapsed = Math.floor((Date.now() - state.solo.startedAt) / 1000);
  $("#solo-timer").textContent = `${String(Math.floor(elapsed / 60)).padStart(2, "0")}:${String(elapsed % 60).padStart(2, "0")}`;
}

function renderDivisionChoices(prefix, question) {
  const wrap = $(`#${prefix}-division-wrap`);
  const container = $(`#${prefix}-divisions`);
  wrap.classList.toggle("hidden", !question.needsDivision);
  container.innerHTML = "";
  question.divisions.forEach((division, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.value = division;
    button.textContent = division;
    button.classList.toggle("active", index === 0);
    button.addEventListener("click", () => selectSegment(container, button));
    container.appendChild(button);
  });
}

function setSoloAnswerMode(mode) {
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
    division: selectedValue(`#${prefix}-divisions`) || "",
  };
}

function solutionText(solution) {
  return solution.answers.map(a => `${escapeHtml(a.contest)} / ${escapeHtml(a.round)}`).join("<br>");
}

function statusText(status) {
  return { pending: "待审核", approved: "已通过", rejected: "未通过" }[status] || status;
}

function submissionItem(item) {
  const media = item.imageUrl
    ? `<div class="submission-thumb"><img src="${escapeHtml(item.imageUrl)}" alt="投稿裁图"></div>`
    : `<div class="submission-thumb">TEXT</div>`;
  return `<article class="submission-item ${escapeHtml(item.status)}">${media}<div class="submission-copy"><strong>${escapeHtml(item.answer)}</strong><span class="status-label">${statusText(item.status)}</span>${item.textClue ? `<p>${escapeHtml(item.textClue)}</p>` : ""}${item.note ? `<p>${escapeHtml(item.note)}</p>` : ""}${item.reviewNote ? `<p>审核：${escapeHtml(item.reviewNote)}</p>` : ""}</div></article>`;
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

async function previewSubmissionFile(event) {
  const file = event.target.files[0];
  state.uploadData = "";
  $("#submission-preview-wrap").classList.add("hidden");
  if (!file) return;
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 3 * 1024 * 1024) {
    event.target.value = "";
    return toast("只支持 3 MB 以内的 PNG、JPEG 或 WebP", "error");
  }
  state.uploadData = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
  $("#submission-preview").src = state.uploadData;
  $("#submission-preview-wrap").classList.remove("hidden");
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
      },
    });
    toast("投稿已进入审核队列");
    $("#submission-form").reset();
    state.uploadData = "";
    $("#submission-preview-wrap").classList.add("hidden");
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
  return `<article class="review-item" data-review-id="${item.id}">${media}<form class="review-form"><div class="full submission-copy"><strong>${escapeHtml(item.answer)} · ${escapeHtml(item.username)}</strong><span>${escapeHtml(item.clueKind)}${item.suggestedBrain ? " · 建议最强大脑" : ""}</span>${item.note ? `<p>${escapeHtml(item.note)}</p>` : ""}</div><label>题名<input name="title" required></label><label>Rating<input name="rating" type="number" min="800" max="4000" step="100" required></label><label>Round<input name="roundNumber" type="number" min="1" required></label><label>组别<select name="division"><option>Div. 1</option><option selected>Div. 2</option><option>Div. 3</option><option>Edu</option><option>Div. 1 + Div. 2</option></select></label><label>比赛日期<input name="contestDate" type="date" value="${today}" required></label><label class="check-line"><input name="brain" type="checkbox" ${item.suggestedBrain ? "checked" : ""}> 最强大脑</label><label class="full">审核备注<textarea name="reviewNote" rows="2"></textarea></label><div class="review-actions"><button class="danger-btn reject-review" type="button">不通过</button><button class="primary approve-review" type="submit">通过并入库</button></div></form></article>`;
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
  setButtonBusy(button, true, "判定中...");
  try {
    const payload = await api("/api/quiz/answer", {
      method: "POST",
      body: { token: state.solo.question.token, ...answerPayload("solo", state.solo.answerMode) },
    });
    clearInterval(state.solo.timer);
    state.user = payload.user;
    renderUser();
    $("#solo-answer-form").classList.add("hidden");
    const result = $("#solo-result");
    result.className = `result-strip ${payload.correct ? "" : "wrong"}`;
    result.innerHTML = `<strong>${payload.correct ? `回答正确，+${payload.points} 分` : "没有命中"}</strong><span>${escapeHtml(payload.solution.title)} · ${payload.solution.rating}</span><br><span>${solutionText(payload.solution)}</span><br><a href="${escapeHtml(payload.solution.sourceUrl)}" target="_blank" rel="noreferrer">查看原题</a> <button id="next-solo" class="text-btn" type="button">下一题</button>`;
    $("#next-solo").addEventListener("click", nextSoloQuestion);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
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
        rounds: Number($("#battle-rounds").value),
        filters: { difficulty: selectedValue("#battle-difficulty") || "medium" },
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
  $("#room-mode").textContent = match.rated ? "Rating 模式" : "娱乐模式";
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
  $("#battle-round-label").textContent = `第 ${match.round} / ${match.rounds} 题`;
  $("#battle-progress").style.width = `${100 * match.round / match.rounds}%`;
  $("#battle-timer").textContent = `${match.secondsLeft}s`;

  if (match.status === "finished") {
    stopBattlePoll();
    $("#battle-answer-form").classList.add("hidden");
    const result = $("#battle-result");
    result.className = "result-strip";
    result.innerHTML = `<strong>对战结束</strong><span>${match.winner === "draw" ? "平局" : `${escapeHtml(match.winner)} 获胜`}</span><br><button id="leave-room" class="text-btn" type="button">返回对战大厅</button>`;
    $("#leave-room").addEventListener("click", leaveRoom);
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

  if (match.phase === "playing") {
    $("#battle-result").className = "result-strip hidden";
    $("#battle-answer-form").classList.toggle("hidden", match.answered);
    if (match.answered) {
      const result = $("#battle-result");
      result.className = "result-strip";
      result.innerHTML = "<strong>已提交</strong><span>等待对手作答或倒计时结束。</span>";
    }
  } else if (match.phase === "reveal") {
    $("#battle-answer-form").classList.add("hidden");
    const result = $("#battle-result");
    const rows = match.reveal.answers.map(a => `${escapeHtml(a.username)}：${a.correct ? `正确 +${a.points}` : "未命中"}`).join(" · ") || "本轮无人作答";
    result.className = "result-strip";
    result.innerHTML = `<strong>${escapeHtml(match.reveal.solution.title)} · ${match.reveal.solution.rating}</strong><span>${solutionText(match.reveal.solution)}</span><br><span>${rows}</span>`;
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
  try {
    const payload = await api(`/api/matches/${state.battle.code}/answer`, {
      method: "POST",
      body: answerPayload("battle", state.battle.answerMode),
    });
    toast(payload.correct ? `抢答正确，+${payload.points} 分` : "没有命中，本题已锁定", payload.correct ? "" : "error");
    await pollBattle();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setButtonBusy(button, false);
  }
}

function leaveRoom() {
  stopBattlePoll();
  state.battle.code = null;
  $("#battle-room").classList.add("hidden");
  $("#battle-setup").classList.remove("hidden");
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
  $$("#solo-difficulty button, #battle-mode button, #battle-difficulty button, #submission-kind button").forEach(btn => btn.addEventListener("click", () => selectSegment(btn.parentElement, btn)));
  $("#solo-start").addEventListener("click", startSolo);
  $$('[data-answer-mode]').forEach(btn => btn.addEventListener("click", () => setSoloAnswerMode(btn.dataset.answerMode)));
  $("#solo-answer-form").addEventListener("submit", submitSoloAnswer);
  $("#refresh-board").addEventListener("click", loadLeaderboard);
  $("#submission-file").addEventListener("change", previewSubmissionFile);
  $("#submission-form").addEventListener("submit", submitClue);
  $("#refresh-submissions").addEventListener("click", loadMySubmissions);
  $("#refresh-reviews").addEventListener("click", loadAdminSubmissions);
  $("#create-room").addEventListener("click", createRoom);
  $("#join-room").addEventListener("click", joinRoom);
  $("#join-code").addEventListener("input", event => { event.target.value = event.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ""); });
  $("#room-code").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(state.battle.code); toast("房间码已复制"); } catch (_) { toast("房间码复制失败", "error"); }
  });
  $("#start-battle").addEventListener("click", startBattle);
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
