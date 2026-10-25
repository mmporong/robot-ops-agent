"use strict";

(() => {
  const app = document.getElementById("incident-app");
  const dataNode = document.getElementById("incident-data");
  if (!app || !dataNode) return;

  const make = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const safeObject = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const safeList = (value) => Array.isArray(value) ? value.filter((item) => item && typeof item === "object") : [];
  const finite = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const fmt = (value, digits = 2) => Number.isFinite(Number(value))
    ? Number(value).toLocaleString("ko-KR", { maximumFractionDigits: digits }) : "—";
  const copy = (value, fallback) => typeof value === "string" && value.trim() ? value.trim() : fallback;
  const logLevel = (value) => {
    const level = typeof value === "string" ? value.trim().toUpperCase() : "";
    if (["ERROR", "FATAL", "CRITICAL"].includes(level)) return { label: "오류", tone: "is-error" };
    if (["WARN", "WARNING"].includes(level)) return { label: "경고", tone: "is-warning" };
    if (level === "DEBUG" || level === "TRACE") return { label: "상세", tone: "is-debug" };
    return { label: "정보", tone: "is-info" };
  };
  const fieldLabels = {
    source_sequence: "센서 측정 순서 번호",
    source_sample_timestamp_s: "센서 원본 측정 시각",
    communication_errors: "통신 오류 기록",
    sensor_publish_period_s: "센서 발행 주기",
    collector_receive_timestamp_s: "수집기 수신 시각",
    actuator_state: "구동기 상태",
    hardware_error_status: "구동기 오류 상태",
    actuator_error_code: "구동기 오류 코드",
    motor_current: "모터 전류",
    controller_state: "제어기 상태",
    torque: "관절 토크",
    temperature: "모터 온도",
    dropped_sample_count: "누락된 샘플 수",
    clock_sync_status: "장치 시계 동기화 상태",
    trajectory_sha256: "궤적 파일 지문",
    time_basis: "로그 시간 기준",
    events: "실행 로그 항목",
    error_code_definition: "오류 코드 정의",
    operator_failure_time_s: "작업자가 확인한 실패 시각",
    task_success_criterion: "작업 성공 기준",
    task_state: "작업 상태 기록",
  };
  const svgNode = (tag, attributes = {}) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  };

  let report;
  try {
    report = JSON.parse(dataNode.textContent || "{}");
  } catch (error) {
    app.append(make("h1", "incident-fatal", "사고 검토 데이터를 열 수 없습니다."));
    return;
  }

  const media = safeObject(report.media);
  const series = safeObject(report.series);
  const times = Array.isArray(series.t) ? series.t.map(Number).filter(Number.isFinite) : [];
  const joints = safeList(series.joints);
  const windows = safeList(report.windows).slice().sort((a, b) => finite(a.start_s) - finite(b.start_s));
  const trace = safeList(report.trace);
  const nextChecks = safeList(report.next_checks);
  const summary = safeObject(report.summary);
  const source = safeObject(report.source);
  const comparison = safeObject(report.comparison);
  const timeStart = times.length ? times[0] : 0;
  const duration = Math.max(0, finite(summary.duration_s, times.length ? times[times.length - 1] - timeStart : 0));
  const timeEnd = timeStart + duration;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const shortStart = (item) => finite(item.start_s, timeStart);
  const shortEnd = (item) => finite(item.end_s, shortStart(item));
  const fullStart = (item) => finite(item.full_start_s, shortStart(item));
  const fullEnd = (item) => finite(item.full_end_s, shortEnd(item));
  const hasLongRange = (item) => fullStart(item) < shortStart(item) - 0.001 || fullEnd(item) > shortEnd(item) + 0.001;
  let currentTime = windows.length ? finite(windows[0].focus_s, finite(windows[0].start_s, timeStart)) : timeStart;
  currentTime = Math.min(timeEnd, Math.max(timeStart, currentTime));
  let selectedJoint = 0;
  let focusedWindow = windows.length ? 0 : -1;
  let expandedWindow = -1;
  let reviewStopTime = null;
  let playing = false;
  let frame = null;
  let lastTick = 0;
  const videos = [];

  const header = make("header", "incident-header");
  const status = report.status === "no_candidate" ? "검사 범위에서 후보 없음" : "사람 검토 필요";
  header.append(make("p", `incident-status ${report.status === "no_candidate" ? "is-clear" : "is-review"}`, status));
  header.append(make("h1", "incident-title", report.status === "no_candidate" ? "검사한 범위에서 사고 후보를 찾지 못했습니다" : "사고 후보의 영상과 신호를 같은 시각에 맞춰 봅니다"));
  const presentedCount = Math.max(0, finite(summary.selected_count, windows.length));
  const summaryLine = report.status === "no_candidate"
    ? "검사하지 못한 신호까지 정상이라는 뜻은 아닙니다. 아래 범위와 다음 확인 항목을 함께 보세요."
    : `전체 ${fmt(summary.window_count, 0)}개 후보 중 ${fmt(presentedCount, 0)}개 구간을 제시했습니다. 기록 전체의 경고는 ${fmt(summary.issue_count, 0)}개이며, 원인은 아직 확정하지 않았습니다.`;
  header.append(make("p", "incident-lead", summaryLine));
  const meta = make("p", "incident-meta");
  meta.append(make("strong", "", copy(source.label, "저장된 실행 기록")));
  meta.append(document.createTextNode(` · ${fmt(duration, 1)}초 · SHA-256 ${copy(source.sha256, "없음")}`));
  header.append(meta);
  app.append(header);

  const layout = make("div", "incident-layout");
  const mediaColumn = make("section", "incident-media-column");
  mediaColumn.setAttribute("aria-labelledby", "incident-evidence-title");
  const mediaHead = make("div", "incident-section-head");
  const mediaTitle = make("h2", "", "해당 시각의 실행 장면");
  mediaTitle.id = "incident-evidence-title";
  mediaHead.append(mediaTitle, make("p", "", "그래프와 영상의 재생 위치가 함께 움직입니다."));
  mediaColumn.append(mediaHead);

  const stage = make("div", `incident-stage ${media.real && media.ghost ? "has-pair" : "has-single"}`);
  const addMedia = (src, title, description, kind) => {
    const figure = make("figure", `incident-video-card is-${kind}`);
    const frameBox = make("div", "incident-video-frame");
    if (typeof src === "string" && src) {
      const video = document.createElement("video");
      video.className = "incident-video";
      video.src = src;
      video.muted = true;
      video.playsInline = true;
      video.preload = "metadata";
      video.setAttribute("aria-label", `${title}: ${description}`);
      video.addEventListener("error", () => {
        frameBox.replaceChildren(make("div", "incident-media-empty", "대응하는 영상 없음"));
      });
      frameBox.append(video);
      videos.push(video);
    } else {
      const empty = make("div", "incident-media-empty", "대응하는 영상 없음");
      empty.setAttribute("role", "img");
      empty.setAttribute("aria-label", `${title} 없음`);
      frameBox.append(empty);
    }
    const caption = make("figcaption", "incident-video-caption");
    caption.append(make("strong", "", title), make("span", "", description));
    figure.append(frameBox, caption);
    stage.append(figure);
  };
  const origin = copy(source.origin, "unknown");
  const primaryMediaCopy = origin === "real_recording"
    ? { title: "실측 영상", description: "현장에서 로봇이 어떻게 움직였는지 확인" }
    : origin === "fault_injection"
      ? { title: "고장 주입 실행 영상", description: "의도적으로 고장을 주입한 시험 장면" }
      : origin === "sim_run"
        ? { title: "시뮬레이션 실행 영상", description: "시뮬레이터에서 재현한 실행 장면" }
        : { title: "실행 영상", description: "기록과 출처가 일치하는 실행 장면" };
  addMedia(media.real, primaryMediaCopy.title, primaryMediaCopy.description, "real");
  if (media.ghost) addMedia(media.ghost, "저장 모델 재생", "같은 시각의 기록된 명령과 상태를 재생", "ghost");
  mediaColumn.append(stage);

  const controls = make("div", "incident-controls");
  const playButton = make("button", "incident-play", "재생");
  playButton.type = "button";
  playButton.setAttribute("aria-pressed", "false");
  const fullPlaybackButton = make("button", "incident-full-playback", "전체 기록 재생");
  fullPlaybackButton.type = "button";
  fullPlaybackButton.hidden = true;
  const seek = document.createElement("input");
  seek.className = "incident-seek";
  seek.type = "range";
  seek.min = String(timeStart);
  seek.max = String(timeEnd);
  seek.step = "0.01";
  seek.value = String(currentTime);
  seek.setAttribute("aria-label", "실행 기록 재생 위치");
  const timeOut = make("output", "incident-time");
  controls.append(playButton, fullPlaybackButton, seek, timeOut);
  mediaColumn.append(controls);
  const playbackHint = make("p", "incident-playback-hint", "구간 버튼을 누르면 먼저 볼 범위로 이동합니다. 일반 재생은 전체 기록을 끝까지 재생합니다.");
  mediaColumn.append(playbackHint);
  mediaColumn.append(make("p", "incident-media-note", copy(media.note_ko, "실행 기록과 시간이 맞는 영상만 표시합니다.")));

  const graph = make("section", "incident-graph");
  const graphHead = make("div", "incident-graph-head");
  graphHead.append(make("h3", "", "명령과 관측 기록"));
  const selectLabel = make("label", "incident-joint-label", "표시할 관절");
  const select = document.createElement("select");
  select.className = "incident-joint-select";
  joints.forEach((joint, index) => {
    const option = make("option", "", copy(joint.label_ko, copy(joint.name, `관절 ${index + 1}`)));
    option.value = String(index);
    select.append(option);
  });
  if (!joints.length) {
    const option = make("option", "", "관절 기록 없음");
    option.value = "0";
    select.append(option);
    select.disabled = true;
  }
  selectLabel.append(select);
  graphHead.append(selectLabel);
  graph.append(graphHead);
  const legend = make("div", "incident-legend");
  legend.append(make("span", "is-command", "명령"), make("span", "is-observation", "관측"), make("span", "is-window", "먼저 볼 구간"), make("span", "is-full-window", "경고 전체 범위"));
  graph.append(legend);
  const chart = svgNode("svg", { viewBox: "0 0 960 300", role: "slider", tabindex: "0", "aria-label": "시간에 따른 관절 명령과 관측 기록", "aria-valuemin": timeStart, "aria-valuemax": timeEnd, "aria-valuenow": currentTime });
  chart.classList.add("incident-chart");
  graph.append(chart);
  mediaColumn.append(graph);
  layout.append(mediaColumn);

  const review = make("aside", "incident-review");
  review.setAttribute("aria-label", "사고 후보 검토");
  const live = make("section", "incident-now");
  live.setAttribute("aria-live", "polite");
  const liveTime = make("p", "incident-now-time");
  const liveTitle = make("h2", "", "현재 시각의 근거");
  const liveFacts = make("ul", "incident-facts");
  const liveLogs = make("div", "incident-live-logs");
  live.append(liveTime, liveTitle, liveFacts, liveLogs);
  review.append(live);

  const windowSection = make("section", "incident-windows");
  const windowHead = make("div", "incident-review-head");
  windowHead.append(make("h2", "", "검토할 구간"));
  windowSection.append(windowHead);
  windowSection.append(make("p", "incident-help", windows.length ? "먼저 볼 구간은 핵심 근거를 빠르게 확인하는 범위입니다. 전체 경고 범위도 잃지 않고 이어서 볼 수 있습니다." : "검사 설정 안에서 표시할 사고 후보가 없습니다."));
  const windowList = make("ol", "incident-window-list");
  const windowCards = [];
  windows.forEach((windowItem, index) => {
    const item = make("li", "incident-window");
    const titleRow = make("div", "incident-window-title-row");
    titleRow.append(make("h3", "", copy(windowItem.title_ko, `후보 구간 ${index + 1}`)));
    item.append(titleRow);
    item.append(make("p", "incident-window-time", `먼저 볼 구간 ${fmt(shortStart(windowItem) - timeStart)}–${fmt(shortEnd(windowItem) - timeStart)}초`));
    item.append(make("p", "incident-window-full-time", `경고 전체 범위 ${fmt(fullStart(windowItem) - timeStart)}–${fmt(fullEnd(windowItem) - timeStart)}초`));
    const facts = make("ul", "incident-window-facts");
    const entries = Array.isArray(windowItem.facts_ko) ? windowItem.facts_ko : [];
    entries.slice(0, 3).forEach((fact) => facts.append(make("li", "", copy(fact, "확인할 근거"))));
    item.append(facts);
    const logs = safeList(windowItem.logs);
    if (logs.length) {
      const logList = make("ul", "incident-window-logs");
      logs.forEach((entry) => {
        const row = make("li", "incident-log-row");
        const level = logLevel(entry.level);
        row.append(make("time", "incident-log-time", `${fmt(finite(entry.t, timeStart) - timeStart)}초`));
        row.append(make("span", `incident-log-level ${level.tone}`, level.label));
        row.append(make("span", "incident-log-message", copy(entry.message, "메시지 없음")));
        logList.append(row);
      });
      item.append(logList);
    }
    const jump = make("button", "incident-jump", "이 구간 보기");
    jump.type = "button";
    jump.addEventListener("click", () => {
      stop();
      focusedWindow = index;
      expandedWindow = -1;
      reviewStopTime = shortEnd(windowItem);
      fullPlaybackButton.hidden = true;
      playbackHint.textContent = `먼저 볼 구간 ${fmt(shortStart(windowItem) - timeStart)}–${fmt(shortEnd(windowItem) - timeStart)}초입니다. 재생하면 구간 끝에서 한 번 멈추며, 이후 전체 기록을 계속 재생할 수 있습니다.`;
      const names = Array.isArray(windowItem.joints) ? windowItem.joints : [];
      const matched = joints.findIndex((joint) => names.includes(joint.name));
      if (matched >= 0) {
        selectedJoint = matched;
        select.value = String(matched);
      }
      setTime(shortStart(windowItem), true);
    });
    const actions = make("div", "incident-window-actions");
    actions.append(jump);
    if (hasLongRange(windowItem)) {
      const fullJump = make("button", "incident-full-range", "경고 전체 범위 보기");
      fullJump.type = "button";
      fullJump.addEventListener("click", () => {
        stop();
        focusedWindow = index;
        expandedWindow = index;
        reviewStopTime = null;
        fullPlaybackButton.hidden = true;
        playbackHint.textContent = `경고 전체 범위 ${fmt(fullStart(windowItem) - timeStart)}–${fmt(fullEnd(windowItem) - timeStart)}초를 그래프에 표시했습니다. 재생하면 전체 기록 끝까지 이어집니다.`;
        setTime(fullStart(windowItem), true);
      });
      actions.append(fullJump);
    }
    item.append(actions);
    windowList.append(item);
    windowCards.push(item);
  });
  windowSection.append(windowList);
  review.append(windowSection);

  const checksSection = make("section", "incident-next");
  checksSection.append(make("h2", "", "원인을 가를 다음 확인"));
  checksSection.append(make("p", "incident-cause", report.cause_status === "determined" ? "원인 확인됨" : "원인 미확정 · 아래 기록이 있어야 후보를 구분할 수 있습니다."));
  const checkList = make("ol", "incident-next-list");
  nextChecks.forEach((item) => {
    const li = make("li", "incident-next-item");
    li.append(make("h3", "", copy(item.title_ko, "추가 기록 확인")));
    li.append(make("p", "", copy(item.reason_ko, "남은 원인 후보를 구분하기 위해 필요합니다.")));
    const fields = Array.isArray(item.required_fields) ? item.required_fields.filter((field) => typeof field === "string" && field) : [];
    if (fields.length) {
      li.append(make("p", "incident-required", `필요한 기록: ${fields.map((field) => fieldLabels[field] || "추가 진단 기록").join(", ")}`));
      const rawFields = make("details", "incident-required-raw");
      rawFields.append(make("summary", "", "기록 필드 이름 보기"));
      const codes = make("p", "", fields.join(", "));
      rawFields.append(codes);
      li.append(rawFields);
    }
    checkList.append(li);
  });
  if (!nextChecks.length) checkList.append(make("li", "incident-next-empty", "추가 확인 항목이 기록되지 않았습니다."));
  checksSection.append(checkList);
  review.append(checksSection);

  if (Object.keys(comparison).length) {
    const compare = make("section", "incident-comparison");
    compare.append(make("h2", "", "변경 전후 비교"));
    compare.append(make("p", `incident-compare-status is-${copy(comparison.status, "unknown")}`, copy(comparison.reason_ko, "비교 결과 설명이 없습니다.")));
    const counts = make("div", "incident-compare-counts");
    counts.append(make("span", "", `변경 전 ${fmt(comparison.before_issue_count, 0)}개`), make("span", "", `변경 후 ${fmt(comparison.after_issue_count, 0)}개`));
    compare.append(counts);
    review.append(compare);
  }

  const details = make("details", "incident-trace");
  details.append(make("summary", "", `검사 실행 기록 ${trace.length}단계`));
  const traceList = make("ol", "incident-trace-list");
  trace.forEach((step) => {
    const li = make("li", "");
    li.append(make("h3", "", copy(step.tool, "검사 도구")));
    li.append(make("p", "", copy(step.reason_ko, "실행 이유 없음")));
    li.append(make("p", "incident-trace-result", copy(step.result_ko, "결과 없음")));
    traceList.append(li);
  });
  details.append(traceList);
  review.append(details);
  layout.append(review);
  app.append(layout);

  const activeWindowAt = (time) => {
    const shortIndex = windows.findIndex((windowItem) => time >= shortStart(windowItem) && time <= shortEnd(windowItem));
    if (shortIndex >= 0) return shortIndex;
    if (expandedWindow >= 0 && time >= fullStart(windows[expandedWindow]) && time <= fullEnd(windows[expandedWindow])) return expandedWindow;
    return -1;
  };
  const nearestIndex = (time) => {
    if (!times.length) return -1;
    let low = 0;
    let high = times.length - 1;
    while (low < high) {
      const mid = Math.floor((low + high) / 2);
      if (times[mid] < time) low = mid + 1; else high = mid;
    }
    if (low > 0 && Math.abs(times[low - 1] - time) <= Math.abs(times[low] - time)) return low - 1;
    return low;
  };
  const updateNow = () => {
    const index = activeWindowAt(currentTime);
    const active = index >= 0 ? windows[index] : null;
    liveTime.textContent = `기록 시작 후 ${fmt(currentTime - timeStart)}초`;
    liveTitle.textContent = active ? copy(active.title_ko, "사고 후보 구간") : "표시된 사고 후보 구간 밖입니다";
    liveFacts.replaceChildren();
    const facts = active && Array.isArray(active.facts_ko) ? active.facts_ko : [];
    if (facts.length) facts.forEach((fact) => liveFacts.append(make("li", "", copy(fact, "확인할 근거"))));
    else liveFacts.append(make("li", "", active ? "이 구간에 기록된 근거 설명이 없습니다." : "슬라이더를 움직이거나 구간을 선택해 근거를 확인하세요."));
    liveLogs.replaceChildren();
    const logs = active ? safeList(active.logs) : [];
    logs.forEach((entry) => {
      const row = make("div", "incident-log-row");
      const level = logLevel(entry.level);
      row.append(make("time", "incident-log-time", `${fmt(finite(entry.t, timeStart) - timeStart)}초`));
      row.append(make("span", `incident-log-level ${level.tone}`, level.label));
      row.append(make("span", "incident-log-message", copy(entry.message, "메시지 없음")));
      liveLogs.append(row);
    });
    const highlightIndex = index >= 0 ? index : focusedWindow;
    windowCards.forEach((card, cardIndex) => card.classList.toggle("is-current", cardIndex === highlightIndex));
  };

  const linePath = (values, x, y) => {
    const count = Math.min(times.length, Array.isArray(values) ? values.length : 0);
    let d = "";
    let open = false;
    for (let i = 0; i < count; i += 1) {
      const value = Number(values[i]);
      if (!Number.isFinite(value)) { open = false; continue; }
      d += `${open ? "L" : "M"}${x(times[i]).toFixed(2)},${y(value).toFixed(2)} `;
      open = true;
    }
    return d.trim();
  };

  const renderGraph = () => {
    chart.replaceChildren();
    chart.setAttribute("aria-valuenow", String(currentTime));
    const joint = joints[selectedJoint];
    if (!joint || times.length < 2) {
      const note = svgNode("text", { x: 480, y: 150, "text-anchor": "middle", class: "incident-chart-empty" });
      note.textContent = "표시할 관절 기록이 없습니다";
      chart.append(note);
      return;
    }
    const left = 58, right = 936, top = 18, bottom = 256;
    const values = [...(Array.isArray(joint.command) ? joint.command : []), ...(Array.isArray(joint.observation) ? joint.observation : [])].map(Number).filter(Number.isFinite);
    let min = values.length ? Math.min(...values) : 0;
    let max = values.length ? Math.max(...values) : 1;
    const pad = Math.max((max - min) * 0.12, 0.5);
    min -= pad; max += pad;
    const x = (time) => left + ((finite(time) - timeStart) / Math.max(duration, 0.001)) * (right - left);
    const y = (value) => bottom - ((value - min) / Math.max(max - min, 0.001)) * (bottom - top);
    for (let i = 0; i <= 4; i += 1) {
      const yy = top + ((bottom - top) * i / 4);
      chart.append(svgNode("line", { x1: left, x2: right, y1: yy, y2: yy, class: "incident-grid-line" }));
      const label = svgNode("text", { x: left - 9, y: yy + 4, "text-anchor": "end", class: "incident-axis" });
      label.textContent = fmt(max - ((max - min) * i / 4), 1);
      chart.append(label);
    }
    if (focusedWindow >= 0) {
      const windowItem = windows[focusedWindow];
      if (expandedWindow === focusedWindow && hasLongRange(windowItem)) {
        chart.append(svgNode("rect", { x: x(fullStart(windowItem)), y: top, width: Math.max(2, x(fullEnd(windowItem)) - x(fullStart(windowItem))), height: bottom - top, class: "incident-full-window-band" }));
      }
      chart.append(svgNode("rect", { x: x(shortStart(windowItem)), y: top, width: Math.max(2, x(shortEnd(windowItem)) - x(shortStart(windowItem))), height: bottom - top, class: "incident-window-band" }));
    }
    chart.append(svgNode("path", { d: linePath(joint.command, x, y), class: "incident-line is-command" }));
    chart.append(svgNode("path", { d: linePath(joint.observation, x, y), class: "incident-line is-observation" }));
    chart.append(svgNode("line", { x1: x(currentTime), x2: x(currentTime), y1: top, y2: bottom, class: "incident-cursor" }));
    const cursorIndex = nearestIndex(currentTime);
    if (cursorIndex >= 0) {
      [[joint.command, "is-command"], [joint.observation, "is-observation"]].forEach(([valuesList, className]) => {
        const value = Number(Array.isArray(valuesList) ? valuesList[cursorIndex] : NaN);
        if (Number.isFinite(value)) chart.append(svgNode("circle", { cx: x(times[cursorIndex]), cy: y(value), r: 5, class: `incident-point ${className}` }));
      });
    }
    const unit = copy(joint.unit, "값");
    const axisLabel = svgNode("text", { x: left, y: 286, class: "incident-axis-title" });
    axisLabel.textContent = `${copy(joint.label_ko, copy(joint.name, "관절"))} · ${unit}`;
    chart.append(axisLabel);
  };

  const syncVideos = (play) => {
    const mediaTime = Math.max(0, currentTime - timeStart);
    videos.forEach((video) => {
      if (Number.isFinite(video.duration) && Math.abs(video.currentTime - mediaTime) > 0.18) {
        video.currentTime = Math.min(mediaTime, video.duration);
      }
      if (play) video.play().catch(() => stop()); else video.pause();
    });
  };

  const setTime = (time, updateMedia) => {
    currentTime = Math.min(timeEnd, Math.max(timeStart, finite(time, timeStart)));
    seek.value = String(currentTime);
    timeOut.textContent = `${fmt(currentTime - timeStart, 1)}초 / ${fmt(duration, 1)}초`;
    if (updateMedia) syncVideos(false);
    updateNow();
    renderGraph();
  };

  const stop = () => {
    playing = false;
    playButton.textContent = "재생";
    playButton.setAttribute("aria-pressed", "false");
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    videos.forEach((video) => video.pause());
  };

  const tick = (stamp) => {
    if (!playing) return;
    if (!lastTick) lastTick = stamp;
    const primary = videos.find((video) => !video.paused && Number.isFinite(video.currentTime));
    const next = primary ? timeStart + primary.currentTime : currentTime + Math.max(0, stamp - lastTick) / 1000;
    lastTick = stamp;
    if (reviewStopTime !== null && next >= reviewStopTime) {
      const boundary = reviewStopTime;
      reviewStopTime = null;
      setTime(boundary, false);
      stop();
      fullPlaybackButton.hidden = false;
      playbackHint.textContent = "먼저 볼 구간의 끝에서 한 번 멈췄습니다. ‘전체 기록 재생’이나 재생 버튼을 누르면 이 시각부터 계속됩니다.";
      return;
    }
    if (next >= timeEnd) { setTime(timeEnd, false); stop(); return; }
    setTime(next, false);
    frame = requestAnimationFrame(tick);
  };

  const start = () => {
    if (duration <= 0) return;
    if (currentTime >= timeEnd - 0.01) setTime(timeStart, true);
    fullPlaybackButton.hidden = true;
    playing = true;
    lastTick = 0;
    playButton.textContent = "일시정지";
    playButton.setAttribute("aria-pressed", "true");
    syncVideos(true);
    frame = requestAnimationFrame(tick);
  };

  playButton.addEventListener("click", () => {
    if (playing) {
      stop();
      return;
    }
    if (!fullPlaybackButton.hidden) playbackHint.textContent = "전체 기록을 현재 시각부터 끝까지 재생합니다.";
    start();
  });
  fullPlaybackButton.addEventListener("click", () => {
    stop();
    reviewStopTime = null;
    fullPlaybackButton.hidden = true;
    playbackHint.textContent = "전체 기록을 현재 시각부터 끝까지 재생합니다.";
    start();
  });
  seek.addEventListener("input", () => {
    stop();
    reviewStopTime = null;
    fullPlaybackButton.hidden = true;
    playbackHint.textContent = "재생 위치를 옮겼습니다. 재생하면 현재 시각부터 전체 기록 끝까지 이어집니다.";
    setTime(Number(seek.value), true);
  });
  select.addEventListener("change", () => { selectedJoint = Number(select.value) || 0; renderGraph(); });
  chart.addEventListener("pointerdown", (event) => {
    const box = chart.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientX - box.left) / Math.max(box.width, 1)));
    stop();
    reviewStopTime = null;
    fullPlaybackButton.hidden = true;
    setTime(timeStart + ratio * duration, true);
  });
  chart.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    stop();
    reviewStopTime = null;
    fullPlaybackButton.hidden = true;
    setTime(currentTime + (event.key === "ArrowRight" ? 0.1 : -0.1), true);
  });
  videos.forEach((video) => {
    video.addEventListener("loadedmetadata", () => setTime(currentTime, true), { once: true });
    video.addEventListener("play", () => { if (!playing) start(); });
    video.addEventListener("pause", () => { if (playing && !video.ended) stop(); });
  });

  if (reducedMotion) app.classList.add("reduce-motion");
  setTime(currentTime, true);
})();
