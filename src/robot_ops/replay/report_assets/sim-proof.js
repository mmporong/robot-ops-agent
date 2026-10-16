"use strict";

(() => {
  const app = document.getElementById("sim-proof-app");
  const dataNode = document.getElementById("sim-proof-data");
  if (!app || !dataNode) return;

  const NS = "http://www.w3.org/2000/svg";
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const make = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined && value !== null) node.textContent = String(value);
    return node;
  };
  const svgNode = (tag, attributes = {}) => {
    const node = document.createElementNS(NS, tag);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  };
  const finite = (value) => value !== null && value !== "" && typeof value !== "boolean" && Number.isFinite(Number(value));
  const number = (value, digits = 2) => finite(value)
    ? Number(value).toLocaleString("ko-KR", { maximumFractionDigits: digits })
    : "확인 불가";
  const safeText = (value, fallback) => typeof value === "string" && value.trim() ? value : fallback;
  const phaseLabel = (phase) => ({
    RESET: "시작 자세 준비",
    REORIENT_ABOVE: "컵 위에서 방향 정렬",
    PREGRASP_ABOVE: "컵 위로 접근",
    ALIGN_MIDDLE: "컵 중간 높이로 정렬",
    APPROACH: "컵에 접근 중",
    PRE_CLOSE: "그리퍼를 닫기 전",
    CLOSE: "그리퍼 닫는 중",
    LIFT: "컵 들어 올리는 중",
    LIFT_HOLD: "들어 올린 컵 유지",
    CONTACT_HOLD: "파지 접촉 유지",
    RETREAT: "접촉 후 후퇴",
    REOBSERVE: "컵 위치 다시 확인",
    DONE: "동작 완료",
  }[String(phase).toUpperCase()] || safeText(phase, "단계 확인 불가"));

  let data;
  try {
    data = JSON.parse(dataNode.textContent || "{}");
  } catch (error) {
    renderFatal("결과 데이터를 읽을 수 없습니다", "파일의 시뮬레이션 결과 형식을 확인해 주세요.");
    return;
  }

  if (data.schema !== "robot-ops-simulation-proof/1" || !Array.isArray(data.stages) || !data.stages.length) {
    renderFatal("표시할 시뮬레이션 실행이 없습니다", "세 조건의 실행 결과와 영상이 포함된 리포트를 다시 만들어 주세요.");
    return;
  }

  const legacyStageOrder = ["normal", "fault", "recovery"];
  const requestedOrder = Array.isArray(data.stage_order)
    ? data.stage_order.filter((id) => typeof id === "string" && id.trim())
    : [];
  const stageOrder = requestedOrder.length ? [...new Set(requestedOrder)] : legacyStageOrder;
  const comparisonMode = stageOrder.some((id) => ["original", "sham", "corrected", "opposite"].includes(id));
  const fallbackLabels = {
    normal: "정상 조건",
    fault: "위치 오류",
    recovery: "복구 확인",
    original: "원래 계획",
    sham: "가짜 보정",
    corrected: "목표 보정",
    opposite: "반대 방향 보정",
  };
  const suppliedStages = data.stages
    .filter((stage) => stage && typeof stage === "object" && stageOrder.includes(stage.id))
    .sort((a, b) => stageOrder.indexOf(a.id) - stageOrder.indexOf(b.id));
  const stages = stageOrder.map((id) => suppliedStages.find((stage) => stage.id === id) || {
    id, label_ko: fallbackLabels[id], samples: [], missing: true,
  });
  const stageLabel = (stage) => comparisonMode && stage.id === "corrected"
    ? "목표 보정"
    : safeText(stage.label_ko, fallbackLabels[stage.id] || "비교 조건");

  const primaryStage = safeText(data.primary_stage, comparisonMode ? "original" : "fault");
  let active = stages.findIndex((stage) => stage.id === primaryStage);
  if (active < 0) active = 0;
  let currentTime = 0;

  const intro = make("header", "sim-proof-intro");
  intro.append(make("p", "sim-proof-kicker", comparisonMode ? "원인 비교 실행" : "시뮬레이션 실행 검증"));
  intro.append(make("h1", "sim-proof-title", safeText(data.title_ko, "컵을 닫기 전에 건드리나요?")));
  intro.append(make("p", "sim-proof-question", safeText(data.question_ko,
    comparisonMode
      ? "실제 컵 위치는 그대로 둔 채 로봇이 목표로 삼는 위치만 바꾸면 실패가 사라지는지 비교합니다."
      : "같은 동작을 정상 조건, 위치 오류, 복구 조건에서 실행해 닫기 전 접촉 여부를 비교합니다.")));
  intro.append(make("p", "sim-proof-execution", safeText(data.execution_ko,
    comparisonMode
      ? "네 조건을 같은 물리 환경에서 실행했습니다. 영상과 아래 좌표·9칸 결과를 함께 확인하세요."
      : "물리 시뮬레이션의 실행 녹화입니다. 검사 도구가 원시 접촉력을 다시 계산했습니다.")));
  app.append(intro);

  const proof = make("div", "sim-proof-workspace");
  const tabs = make("div", "sim-proof-tabs");
  tabs.style.setProperty("--sim-proof-stage-count", String(stages.length));
  tabs.classList.toggle("is-comparison", comparisonMode);
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "비교할 시뮬레이션 조건");
  const tabButtons = stages.map((stage, index) => {
    const button = make("button", "sim-proof-tab");
    button.type = "button";
    button.id = `sim-proof-tab-${stage.id}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", "sim-proof-panel");
    button.setAttribute("aria-selected", String(index === active));
    button.tabIndex = index === active ? 0 : -1;
    button.append(make("span", "sim-proof-tab-label", stageLabel(stage)));
    const isValid = stageInputValid(stage);
    const isFailed = isValid && stageFailed(stage);
    const recovered = stage.id === "recovery" && stage.verdict === "pass";
    button.append(make("span", `sim-proof-tab-verdict is-${isValid ? (recovered || !isFailed ? "pass" : "fail") : "missing"}`,
      isValid ? (recovered ? "복구 통과" : isFailed ? "실패" : "통과") : "입력 누락"));
    button.addEventListener("click", () => selectStage(index, true));
    button.addEventListener("keydown", (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      let next = index;
      if (event.key === "ArrowLeft") next = (index - 1 + stages.length) % stages.length;
      if (event.key === "ArrowRight") next = (index + 1) % stages.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = stages.length - 1;
      selectStage(next, true);
      tabButtons[next].focus();
    });
    tabs.append(button);
    return button;
  });
  proof.append(tabs);

  const panel = make("section", "sim-proof-panel");
  panel.id = "sim-proof-panel";
  panel.setAttribute("role", "tabpanel");
  const videoFrame = make("div", "sim-proof-video-frame");
  const video = document.createElement("video");
  video.className = "sim-proof-video";
  video.muted = true;
  video.playsInline = true;
  video.preload = "metadata";
  video.setAttribute("aria-label", "선택한 조건의 시뮬레이션 실행 영상");
  let detailZoom = comparisonMode;
  video.classList.toggle("is-detail-zoom", detailZoom);
  const videoError = make("div", "sim-proof-video-error");
  videoError.setAttribute("role", "alert");
  videoError.hidden = true;
  const overlay = make("div", "sim-proof-overlay");
  const overlayPhase = make("span", "sim-proof-overlay-phase");
  const overlayVerdict = make("strong", "sim-proof-overlay-verdict");
  overlay.append(overlayPhase, overlayVerdict);
  const zoomButton = make("button", "sim-proof-zoom", "전체 장면 보기");
  zoomButton.type = "button";
  zoomButton.hidden = !comparisonMode;
  zoomButton.setAttribute("aria-pressed", String(detailZoom));
  zoomButton.setAttribute("aria-label", "컵과 그리퍼 영역 확대 전환");
  zoomButton.addEventListener("click", () => {
    detailZoom = !detailZoom;
    video.classList.toggle("is-detail-zoom", detailZoom);
    zoomButton.setAttribute("aria-pressed", String(detailZoom));
    zoomButton.textContent = detailZoom ? "전체 장면 보기" : "컵·그리퍼 확대";
  });
  const sceneContext = make("div", "sim-proof-scene-context");
  sceneContext.hidden = !comparisonMode;
  const sceneLegend = make("span", "sim-proof-scene-legend", "노란 원통: 컵 · 회색 팔: 로봇");
  const sceneDescription = make("span", "sim-proof-scene-description");
  sceneContext.append(sceneLegend, sceneDescription);
  videoFrame.append(video, videoError, zoomButton, sceneContext, overlay);
  panel.append(videoFrame);

  const controls = make("div", "sim-proof-controls");
  const play = make("button", "sim-proof-play", "재생");
  play.type = "button";
  play.setAttribute("aria-pressed", "false");
  const seek = document.createElement("input");
  seek.className = "sim-proof-seek";
  seek.type = "range";
  seek.min = "0";
  seek.step = "0.001";
  seek.value = "0";
  seek.setAttribute("aria-label", "시뮬레이션 재생 위치");
  const time = make("output", "sim-proof-time", "0.00초");
  controls.append(play, seek, time);
  panel.append(controls);

  const readout = make("section", "sim-proof-readout");
  readout.setAttribute("aria-label", "현재 시각 측정값");
  const phaseValue = metric("현재 단계");
  const forceValue = metric("닫기 전 접촉력");
  const thresholdValue = metric("검출 임계값");
  const heightValue = metric("컵 상승량");
  readout.append(phaseValue.root, forceValue.root, thresholdValue.root, heightValue.root);
  panel.append(readout);

  const diagnosisBox = make("section", "sim-proof-diagnosis");
  diagnosisBox.hidden = !comparisonMode || !data.diagnosis || typeof data.diagnosis !== "object";
  panel.append(diagnosisBox);

  const verdictBox = make("section", "sim-proof-answer");
  const verdictTitle = make("h2", "sim-proof-answer-title");
  const verdictDescription = make("p", "sim-proof-answer-copy");
  const jump = make("button", "sim-proof-jump", "문제 시점 보기");
  jump.type = "button";
  jump.addEventListener("click", () => {
    const primaryIndex = stages.findIndex((stage) => stage.id === primaryStage && stageFailed(stage));
    const faultIndex = primaryIndex >= 0 ? primaryIndex : stages.findIndex((stage) => stageFailed(stage));
    if (faultIndex < 0) return;
    selectStage(faultIndex, false);
    const rawEventTime = stages[faultIndex].event_t_s;
    seekTo(finite(rawEventTime) ? Number(rawEventTime) : firstDetectedTime(stages[faultIndex]), true);
    panel.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
  });
  verdictBox.append(verdictTitle, verdictDescription, jump);
  panel.append(verdictBox);

  const graphSection = make("section", "sim-proof-graph-section");
  const graphTitle = make("h2", "sim-proof-section-title", "닫기 전 접촉력");
  const legend = make("p", "sim-proof-legend", "파랑: 닫기 전 측정값 · 빨강: 임계값을 넘은 실제 샘플");
  const chart = svgNode("svg", {
    class: "sim-proof-chart",
    viewBox: "0 0 960 250",
    role: "slider",
    tabindex: "0",
    "aria-label": "시간에 따른 닫기 전 접촉력 그래프",
    "aria-valuemin": "0",
    "aria-valuenow": "0",
  });
  chart.addEventListener("click", (event) => {
    const bounds = chart.getBoundingClientRect();
    const ratio = ((event.clientX - bounds.left) / bounds.width * 960 - 54) / 872;
    seekTo(ratio * duration(), true);
  });
  chart.addEventListener("keydown", (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Home") seekTo(0, true);
    else if (event.key === "End") seekTo(duration(), true);
    else seekTo(currentTime + (event.key === "ArrowRight" ? 0.05 : -0.05), true);
  });
  graphSection.append(graphTitle, legend, chart);
  panel.append(graphSection);

  const details = make("details", "sim-proof-details");
  const detailsSummary = make("summary", "", "실행 범위와 근거 확인");
  const detailsBody = make("div", "sim-proof-details-body");
  details.append(detailsSummary, detailsBody);
  panel.append(details);
  proof.append(panel);
  app.append(proof);

  renderDiagnosis();

  play.addEventListener("click", () => video.paused ? startPlayback() : video.pause());
  seek.addEventListener("input", () => seekTo(Number(seek.value), true));
  video.addEventListener("timeupdate", () => updateAt(video.currentTime));
  video.addEventListener("play", updatePlaybackState);
  video.addEventListener("pause", updatePlaybackState);
  video.addEventListener("ended", updatePlaybackState);
  video.addEventListener("error", () => showVideoError("이 실행의 영상을 불러오지 못했습니다. 결과 파일과 media 경로를 확인해 주세요."));
  video.addEventListener("loadedmetadata", () => {
    seek.max = String(duration());
    if (currentTime > 0) video.currentTime = Math.min(currentTime, duration());
  });

  selectStage(active, !reducedMotion);

  function metric(label) {
    const root = make("div", "sim-proof-metric");
    const name = make("span", "sim-proof-metric-label", label);
    const value = make("strong", "sim-proof-metric-value", "—");
    root.append(name, value);
    return { root, value };
  }

  function samples(stage = stages[active]) {
    return Array.isArray(stage.samples) ? stage.samples.filter((sample) => sample && finite(sample.t_s)) : [];
  }

  function stageInputValid(stage = stages[active]) {
    if (stage.missing || !samples(stage).length) return false;
    return comparisonMode
      ? ["pass", "fail"].includes(stage.verdict) || finite(stage.force_threshold_n)
      : finite(stage.force_threshold_n);
  }

  function stageFailed(stage = stages[active]) {
    if (stage.verdict === "fail") return true;
    if (stage.verdict === "pass") return false;
    return detectedSamples(stage).length > 0;
  }

  function detectedSamples(stage = stages[active]) {
    if (!finite(stage.force_threshold_n)) return [];
    const threshold = Number(stage.force_threshold_n);
    return samples(stage).filter((sample) => sample.preclose === true && finite(sample.force_n) && Number(sample.force_n) >= threshold);
  }

  function firstDetectedTime(stage) {
    const hit = detectedSamples(stage)[0];
    return hit ? Number(hit.t_s) : 0;
  }

  function duration(stage = stages[active]) {
    const rows = samples(stage);
    return Math.max(0, finite(stage.sim_time_s) ? Number(stage.sim_time_s) : (rows.length ? Number(rows[rows.length - 1].t_s) : 0));
  }

  function nearestSample(at, stage = stages[active]) {
    const rows = samples(stage);
    if (!rows.length) return null;
    return rows.reduce((best, row) => Math.abs(Number(row.t_s) - at) < Math.abs(Number(best.t_s) - at) ? row : best, rows[0]);
  }

  function selectStage(index, autoplay) {
    if (!stages[index]) return;
    active = index;
    currentTime = 0;
    tabButtons.forEach((button, buttonIndex) => {
      button.setAttribute("aria-selected", String(buttonIndex === active));
      button.tabIndex = buttonIndex === active ? 0 : -1;
    });
    panel.setAttribute("aria-labelledby", tabButtons[active].id);
    const stage = stages[active];
    sceneDescription.textContent = safeText(stage.description_ko, "선택한 조건의 실행입니다.");
    video.pause();
    video.removeAttribute("src");
    video.load();
    if (typeof stage.video === "string" && stage.video.trim()) {
      video.hidden = false;
      videoError.hidden = true;
      video.src = stage.video;
      video.load();
      if (autoplay && !reducedMotion) video.play().catch(() => updatePlaybackState());
    } else {
      showVideoError("이 실행에는 시뮬레이션 영상이 없습니다. 같은 실행에서 녹화된 영상을 포함해 주세요.");
    }
    seek.max = String(duration());
    seek.value = "0";
    renderVerdict();
    renderChart();
    renderDetails();
    updateAt(0);
  }

  function showVideoError(message) {
    video.hidden = true;
    videoError.hidden = false;
    videoError.textContent = message;
    updatePlaybackState();
  }

  function startPlayback() {
    if (!video.src || video.hidden) return;
    video.play().catch(() => updatePlaybackState());
  }

  function updatePlaybackState() {
    const playing = !video.paused && !video.ended;
    play.textContent = playing ? "일시정지" : "재생";
    play.setAttribute("aria-pressed", String(playing));
    play.disabled = video.hidden || !video.src;
  }

  function seekTo(value, pause) {
    const next = Math.min(duration(), Math.max(0, Number(value) || 0));
    currentTime = next;
    if (pause) video.pause();
    if (!video.hidden && video.src) video.currentTime = next;
    updateAt(next);
  }

  function updateAt(at) {
    currentTime = Math.min(duration(), Math.max(0, Number(at) || 0));
    seek.value = String(currentTime);
    time.textContent = `${number(currentTime, 2)}초 / ${number(duration(), 2)}초`;
    chart.setAttribute("aria-valuemax", String(duration()));
    chart.setAttribute("aria-valuenow", String(currentTime));
    const sample = nearestSample(currentTime);
    const threshold = finite(stages[active].force_threshold_n) ? Number(stages[active].force_threshold_n) : NaN;
    const detected = Boolean(stageInputValid() && sample && sample.preclose === true && finite(sample.force_n) && Number(sample.force_n) >= threshold);
    const failed = stageFailed();
    overlayPhase.textContent = sample ? phaseLabel(sample.phase) : "측정값 없음";
    overlayVerdict.textContent = detected
      ? "닫기 전 접촉 검출"
      : failed ? "실행 실패 · 결과 확인" : "실행 통과";
    overlayVerdict.classList.toggle("is-fail", detected || failed);
    phaseValue.value.textContent = sample ? phaseLabel(sample.phase) : "측정값 없음";
    forceValue.value.textContent = sample && sample.preclose === true ? `${number(sample.force_n)} N` : "닫기 이후 · 판정 제외";
    thresholdValue.value.textContent = finite(threshold) ? `${number(threshold)} N` : "확인 불가";
    heightValue.value.textContent = sample && finite(sample.cup_z_mm) ? `${number(sample.cup_z_mm)} mm` : "확인 불가";
    positionCursor();
  }

  function renderVerdict() {
    const stage = stages[active];
    if (!stageInputValid(stage)) {
      verdictBox.classList.remove("is-fail");
      verdictBox.classList.add("is-missing");
      verdictTitle.textContent = "이 조건은 판정할 수 없습니다.";
      verdictDescription.textContent = "시간별 접촉력 샘플과 검출 임계값을 포함해 결과를 다시 만들어 주세요.";
      jump.hidden = !stages.some((item) => item.id === "fault" && stageInputValid(item) && detectedSamples(item).length);
      return;
    }
    const hits = detectedSamples(stage);
    const failed = stageFailed(stage);
    const recovered = stage.id === "recovery" && stage.verdict === "pass";
    verdictBox.classList.remove("is-missing");
    verdictBox.classList.toggle("is-fail", failed && !recovered);
    verdictTitle.textContent = safeText(stage.result_title_ko,
      recovered
        ? "조기 접촉 뒤 후퇴·재접근해 컵 상승 검사를 통과했습니다."
        : failed
          ? (hits.length ? "그리퍼를 닫기 전에 컵을 건드려 실행이 멈췄습니다." : "이 실행은 과제를 통과하지 못했습니다.")
          : comparisonMode ? "컵 상승 검사를 통과했습니다." : "닫기 전 접촉 없이 컵 상승 검사를 통과했습니다.");
    verdictDescription.textContent = safeText(stage.result_description_ko,
      safeText(stage.description_ko,
        failed
          ? (hits.length ? `${number(hits.length, 0)}개 실제 샘플이 접촉력 임계값을 넘었습니다.` : "조기 접촉 여부와 관계없이 전체 과제 판정은 실패입니다.")
          : "전체 과제 판정이 통과입니다."));
    jump.textContent = comparisonMode ? "이 실행의 판정 시점 보기" : "문제 시점 보기";
    jump.hidden = comparisonMode
      ? !stages.some((item) => stageInputValid(item) && stageFailed(item))
      : !stages.some((item) => item.id === "fault" && detectedSamples(item).length);
  }

  function renderChart() {
    chart.replaceChildren();
    const stage = stages[active];
    const rows = samples(stage);
    const threshold = finite(stage.force_threshold_n) ? Number(stage.force_threshold_n) : NaN;
    const maxForce = Math.max(1, Number.isFinite(threshold) ? threshold : 0, ...rows.filter((row) => row.preclose === true && finite(row.force_n)).map((row) => Number(row.force_n)));
    const x = (value) => 54 + (Number(value) / Math.max(duration(), 0.001)) * 872;
    const y = (value) => 208 - (Number(value) / maxForce) * 166;
    [0, 0.5, 1].forEach((ratio) => {
      const lineY = 208 - ratio * 166;
      chart.append(svgNode("line", { x1: 54, y1: lineY, x2: 926, y2: lineY, class: "sim-proof-grid-line" }));
      const label = svgNode("text", { x: 46, y: lineY + 4, class: "sim-proof-axis", "text-anchor": "end" });
      label.textContent = number(maxForce * ratio, 1);
      chart.append(label);
    });
    const path = rows.reduce((result, row) => {
      if (row.preclose !== true || !finite(row.force_n)) return `${result} `;
      const prefix = !result.trim() || result.endsWith(" ") ? "M" : "L";
      return `${result}${prefix}${x(row.t_s).toFixed(2)},${y(row.force_n).toFixed(2)}`;
    }, "");
    chart.append(svgNode("path", { d: path, class: "sim-proof-force-line" }));
    if (Number.isFinite(threshold)) {
      chart.append(svgNode("line", { x1: 54, y1: y(threshold), x2: 926, y2: y(threshold), class: "sim-proof-threshold-line" }));
    }
    detectedSamples(stage).forEach((row) => chart.append(svgNode("circle", {
      cx: x(row.t_s), cy: y(row.force_n), r: 5, class: "sim-proof-detected-point",
    })));
    const eventTime = stage.event_t_s;
    if (finite(eventTime)) chart.append(svgNode("rect", {
      x: Math.max(54, x(eventTime) - 6), y: 31, width: 12, height: 187, class: "sim-proof-event-band",
    }));
    chart.append(svgNode("line", { x1: 54, y1: 31, x2: 54, y2: 218, class: "sim-proof-cursor" }));
    const axis = svgNode("text", { x: 926, y: 239, class: "sim-proof-axis", "text-anchor": "end" });
    axis.textContent = `${number(duration(), 1)}초`;
    chart.append(axis);
  }

  function positionCursor() {
    const cursor = chart.querySelector(".sim-proof-cursor");
    if (!cursor) return;
    const x = 54 + (currentTime / Math.max(duration(), 0.001)) * 872;
    cursor.setAttribute("x1", String(x));
    cursor.setAttribute("x2", String(x));
  }

  function vector(value) {
    return Array.isArray(value) && value.length >= 2 && value.every(finite)
      ? value.map((item) => Number(item))
      : null;
  }

  function millimetres(value) {
    return value ? value.slice(0, 2).map((item) => item * 1000) : null;
  }

  function coordinateText(value, unit = "m") {
    if (!value) return "확인 불가";
    const digits = unit === "mm" ? 1 : 3;
    return `X ${number(value[0], digits)} · Y ${number(value[1], digits)} ${unit}`;
  }

  function renderDiagnosis() {
    if (diagnosisBox.hidden) return;
    const diagnosis = data.diagnosis;
    const planned = vector(diagnosis.planned_center_m);
    const observed = vector(diagnosis.observed_center_m);
    const correction = vector(diagnosis.correction_mm);
    const delta = planned && observed
      ? [observed[0] - planned[0], observed[1] - planned[1]]
      : null;

    diagnosisBox.replaceChildren();
    const heading = make("div", "sim-proof-diagnosis-heading");
    heading.append(
      make("p", "sim-proof-diagnosis-kicker", "원인 비교 결과"),
      make("h2", "sim-proof-diagnosis-title", safeText(
        diagnosis.summary_ko,
        "계획 위치와 관측 위치의 차이를 보정했을 때 같은 실패가 사라지는지 확인했습니다."
      )),
    );
    diagnosisBox.append(heading);

    const coordinates = make("div", "sim-proof-coordinate-grid");
    [
      ["로봇이 목표로 삼은 위치", coordinateText(planned)],
      ["시뮬레이터에서 확인한 컵 위치", coordinateText(observed)],
      ["두 위치의 차이", coordinateText(millimetres(delta), "mm")],
      ["목표에 적용한 보정", coordinateText(correction, "mm")],
    ].forEach(([label, value], index) => {
      const card = make("div", `sim-proof-coordinate${index >= 2 ? " is-emphasis" : ""}`);
      card.append(make("span", "sim-proof-coordinate-label", label), make("strong", "sim-proof-coordinate-value", value));
      coordinates.append(card);
    });
    diagnosisBox.append(coordinates);

    const coreSummary = Array.isArray(diagnosis.core_summary) ? diagnosis.core_summary : [];
    if (coreSummary.length) {
      const core = make("div", "sim-proof-core-checks");
      const repeats = finite(diagnosis.core_repetitions) ? `${number(diagnosis.core_repetitions, 0)}회 반복` : "반복 횟수 확인 불가";
      core.append(make("h3", "sim-proof-subtitle", `핵심 조건 · ${repeats}`));
      const rows = make("div", "sim-proof-core-grid");
      coreSummary.forEach((item) => {
        if (!item || typeof item !== "object") return;
        const passed = finite(item.passed) ? Number(item.passed) : 0;
        const total = finite(item.total) ? Number(item.total) : 0;
        const row = make("div", `sim-proof-core-row${total > 0 && passed === total ? " is-pass" : " is-fail"}`);
        row.append(
          make("span", "", safeText(item.label_ko, "검사 조건")),
          make("strong", "", `${number(passed, 0)} / ${number(total, 0)} 통과`),
        );
        rows.append(row);
      });
      core.append(rows);
      diagnosisBox.append(core);
    }

    const regression = Array.isArray(diagnosis.regression) ? diagnosis.regression : [];
    if (regression.length) {
      const regressionSection = make("div", "sim-proof-regression");
      regressionSection.append(
        make("h3", "sim-proof-subtitle", `${number(regression.length, 0)}개 위치 비교`),
        make("p", "sim-proof-regression-help", "같은 컵 위치에서 원래 목표와 보정한 목표의 과제 통과 여부를 비교했습니다."),
      );
      const grid = make("div", "sim-proof-regression-grid");
      regression.forEach((cell) => {
        if (!cell || typeof cell !== "object") return;
        const baseline = cell.baseline_pass === true;
        const corrected = cell.corrected_pass === true;
        const node = make("div", `sim-proof-regression-cell${corrected ? " is-pass" : " is-fail"}`);
        node.append(
          make("span", "sim-proof-regression-position", `X ${number(cell.x_mm, 0)} · Y ${number(cell.y_mm, 0)} mm`),
          make("strong", "sim-proof-regression-result", `${baseline ? "통과" : "실패"} → ${corrected ? "통과" : "실패"}`),
        );
        grid.append(node);
      });
      regressionSection.append(grid);
      diagnosisBox.append(regressionSection);
    }

    const limitations = Array.isArray(diagnosis.limitations_ko) ? diagnosis.limitations_ko : [];
    if (limitations.length) {
      const note = make("div", "sim-proof-diagnosis-note");
      note.append(make("strong", "", "이 결과가 말하지 않는 것"));
      const list = make("ul", "");
      limitations.forEach((item) => list.append(make("li", "", safeText(item, "제약 정보 없음"))));
      note.append(list);
      diagnosisBox.append(note);
    }
  }

  function renderDetails() {
    const stage = stages[active];
    const scope = data.scope && typeof data.scope === "object" ? data.scope : {};
    const source = stage.source && typeof stage.source === "object" ? stage.source : {};
    const trace = data.tool_trace && typeof data.tool_trace === "object" ? data.tool_trace : {};
    detailsBody.replaceChildren();
    detailsBody.append(detail("검증 조건", safeText(scope.basis_ko, "검증 조건 정보 없음")));
    detailsBody.append(detail("실행 방식", safeText(scope.execution_ko, "실행 방식 정보 없음")));
    detailsBody.append(detail("실행 표본", `${number(stage.sample_count, 0)}개 기록 · ${number(stage.sim_time_s, 2)}초`));
    const limits = Array.isArray(scope.limitations_ko) ? scope.limitations_ko : [];
    if (limits.length) {
      const list = make("ul", "sim-proof-limitations");
      limits.forEach((item) => list.append(make("li", "", safeText(item, "제약 정보 없음"))));
      detailsBody.append(list);
    }
    const analysis = stage.analysis && typeof stage.analysis === "object" ? stage.analysis : {};
    const analysisEntries = Object.entries(analysis).filter(([, value]) => ["string", "number", "boolean"].includes(typeof value));
    if (analysisEntries.length) {
      const analysisList = make("dl", "sim-proof-analysis");
      analysisEntries.forEach(([key, value]) => analysisList.append(make("dt", "", key), make("dd", "", String(value))));
      detailsBody.append(analysisList);
    }
    const provenance = make("dl", "sim-proof-provenance");
    [["실행 ID", source.run_id], ["결과 해시", source.result_sha256], ["계획 해시", source.plan_sha256], ["분석 호출", finite(trace.calls) ? `${number(trace.calls, 0)}회 · ${safeText(trace.tool, "도구 정보 없음")} · ${safeText(trace.transport, "전송 정보 없음")}` : "확인 불가"]]
      .forEach(([label, value]) => {
        provenance.append(make("dt", "", label), make("dd", "", safeText(value, "확인 불가")));
      });
    detailsBody.append(provenance);
  }

  function detail(label, value) {
    const row = make("p", "sim-proof-detail");
    row.append(make("strong", "", label), document.createTextNode(value));
    return row;
  }

  function renderFatal(title, description) {
    app.replaceChildren();
    const error = make("main", "sim-proof-fatal");
    error.append(make("h1", "", title), make("p", "", description));
    app.append(error);
  }
})();
