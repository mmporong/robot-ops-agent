"use strict";

(() => {
  const dataNode = document.getElementById("inspection-data");
  const app = document.getElementById("inspection-app");
  if (!dataNode || !app) return;

  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  };
  const number = (value, digits = 1) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toLocaleString("ko-KR", { maximumFractionDigits: digits }) : "—";
  };
  const seconds = (value) => `${number(value, 2)}초`;
  const text = (value, fallback = "확인할 수 없음") =>
    typeof value === "string" && value.trim() ? value : fallback;

  let payload;
  try {
    payload = JSON.parse(dataNode.textContent || "{}");
  } catch (error) {
    app.append(el("h1", "inspection-title", "검사 결과를 열 수 없습니다"));
    app.append(el("p", "inspection-lead", "리포트 데이터 형식이 올바르지 않습니다."));
    return;
  }

  const check = payload.check && typeof payload.check === "object" ? payload.check : {};
  const summary = payload.summary && typeof payload.summary === "object" ? payload.summary : {};
  const source = payload.source && typeof payload.source === "object" ? payload.source : {};
  const media = payload.media && typeof payload.media === "object" ? payload.media : {};
  const series = payload.series && typeof payload.series === "object" ? payload.series : {};
  const joints = Array.isArray(series.joints) ? series.joints.filter((joint) => joint && typeof joint === "object") : [];
  const times = Array.isArray(series.t) ? series.t.map(Number) : [];
  const timeOrigin = times.length ? times[0] : 0;
  const elapsed = (time) => Number(time) - timeOrigin;
  const issues = Array.isArray(check.issues) ? check.issues.filter((issue) => issue && typeof issue === "object") : [];
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const statusCopy = {
    review_required: {
      label: "확인할 구간이 있습니다",
      headline: "관절 상태 기록이 명령을 따라가지 않은 구간을 찾았습니다.",
      description: "표시된 시간의 실물 영상과 그래프를 함께 보고, 기록 문제인지 로봇이 움직이지 않은 것인지 확인해야 합니다.",
      tone: "is-review",
    },
    no_issue_in_checked_scope: {
      label: "검사 범위에서 경고 없음",
      headline: "이번 검사가 확인한 신호에서는 경고 구간이 없었습니다.",
      description: "검사하지 못한 신호와 원본 센서 갱신 여부까지 정상이라고 뜻하지는 않습니다.",
      tone: "is-clear",
    },
    invalid_input: {
      label: "검사할 수 없음",
      headline: "입력 기록을 검사하지 못했습니다.",
      description: "기록 형식과 필수 관절·시간 데이터를 확인해 주세요.",
      tone: "is-invalid",
    },
  };
  const status = statusCopy[payload.status || check.status] || statusCopy.invalid_input;
  let outcome = status.headline;
  if ((payload.status || check.status) === "review_required" && !issues.some((issue) => issue.kind === "joint_observation_stagnation")) {
    outcome = "기록 간격 또는 센서 측정 식별자에서 확인할 구간을 찾았습니다.";
  }

  const header = el("header", "inspection-intro");
  header.append(el("p", `inspection-status ${status.tone}`, status === statusCopy.invalid_input ? status.label : `실험용 검사 · ${status.label}`));
  header.append(el("h1", "inspection-title", text(payload.title_ko, "수집 기록에서 확인할 구간을 찾습니다")));
  header.append(el("p", "inspection-outcome", outcome));
  header.append(el("p", "inspection-lead", status.description));
  if ((payload.status || check.status) === "invalid_input" && payload.error_ko) {
    header.append(el("p", "inspection-error", `확인할 내용: ${text(payload.error_ko)}`));
  }
  const sourceLine = el("p", "inspection-source");
  sourceLine.append(el("span", "inspection-source-label", text(source.label, "수집 기록")));
  sourceLine.append(document.createTextNode(` · ${number(summary.frame_count, 0)}프레임 · ${seconds(summary.duration_s)}`));
  header.append(sourceLine);
  const changeTimes = Array.isArray(summary.observation_change_times_s)
    ? summary.observation_change_times_s.filter((value) => Number.isFinite(Number(value)))
    : [];
  if (Number.isFinite(Number(summary.observation_unique_poses))) {
    const poseSummary = el("p", "inspection-pose-summary");
    poseSummary.append(el("strong", "", `저장된 관절 자세 ${number(summary.observation_unique_poses, 0)}가지`));
    let changeCopy = " · 자세 변화 시점이 기록되지 않았습니다.";
    if (changeTimes.length && changeTimes.length <= 3) {
      changeCopy = ` · ${changeTimes.map((value) => number(elapsed(value), 2)).join(", ")}초에 값이 바뀌었습니다.`;
    } else if (changeTimes.length) {
      changeCopy = ` · 기록 중 ${number(changeTimes.length, 0)}회 바뀌었습니다. 첫 변화는 ${number(elapsed(changeTimes[0]), 2)}초입니다.`;
    }
    poseSummary.append(document.createTextNode(changeCopy));
    header.append(poseSummary);
  }
  app.append(header);

  const workspace = el("div", "inspection-workspace");
  const evidence = el("section", "inspection-evidence");
  evidence.setAttribute("aria-labelledby", "evidence-title");
  const evidenceHead = el("div", "inspection-section-head");
  const evidenceTitle = el("h2", "", "영상과 관절 기록");
  evidenceTitle.id = "evidence-title";
  evidenceHead.append(evidenceTitle);
  evidenceHead.append(el("p", "", "같은 시각의 화면과 수치를 함께 봅니다."));
  evidence.append(evidenceHead);

  const stage = el("div", "inspection-stage");
  const videos = [];
  const mediaSources = [
    { src: media.real, title: "실물 녹화", note: "실제로 로봇이 움직였는지 확인" },
    { src: media.ghost, title: "기록 재생", note: "파랑: 목표 명령 · 노랑: 저장된 상태" },
  ];
  mediaSources.forEach((item) => {
    const figure = el("figure", "inspection-video-card");
    const caption = el("figcaption", "inspection-video-caption");
    caption.append(el("strong", "", item.title));
    caption.append(el("span", "", item.note));
    if (typeof item.src === "string" && item.src) {
      const video = document.createElement("video");
      video.className = "inspection-video";
      video.src = item.src;
      video.muted = true;
      video.playsInline = true;
      video.preload = "metadata";
      video.setAttribute("aria-label", `${item.title}: ${item.note}`);
      figure.append(video, caption);
      videos.push(video);
    } else {
      const empty = el("div", "inspection-video-empty", `${item.title}가 이 기록에 없습니다.`);
      empty.setAttribute("role", "img");
      empty.setAttribute("aria-label", `${item.title} 없음`);
      figure.append(empty, caption);
    }
    stage.append(figure);
  });
  evidence.append(stage);

  const controls = el("div", "inspection-controls");
  const playButton = el("button", "inspection-play", reducedMotion ? "재생" : "일시정지");
  playButton.type = "button";
  playButton.setAttribute("aria-pressed", reducedMotion ? "false" : "true");
  const timeOutput = el("output", "inspection-time", `0.0초 / ${number(summary.duration_s, 1)}초`);
  const sliderLabel = el("label", "inspection-sr-only", "재생 위치");
  const slider = document.createElement("input");
  slider.className = "inspection-seek";
  slider.type = "range";
  slider.min = "0";
  slider.max = String(Math.max(0, Number(summary.duration_s) || (times.length ? times[times.length - 1] : 0)));
  slider.step = "0.01";
  slider.value = "0";
  slider.setAttribute("aria-label", "재생 위치");
  controls.append(playButton, sliderLabel, slider, timeOutput);
  evidence.append(controls);

  const mediaNote = el("p", "inspection-media-note", text(media.note_ko, "영상은 같은 기록의 관절 데이터와 시간을 맞춰 확인합니다."));
  evidence.append(mediaNote);

  const graph = el("section", "inspection-graph");
  graph.setAttribute("aria-labelledby", "graph-title");
  const graphHead = el("div", "inspection-graph-head");
  const graphTitle = el("h3", "", "관절 명령과 저장 상태");
  graphTitle.id = "graph-title";
  const jointSelectLabel = el("label", "inspection-joint-label", "표시할 관절");
  const jointSelect = document.createElement("select");
  jointSelect.className = "inspection-joint-select";
  joints.forEach((joint, index) => {
    const option = el("option", "", text(joint.label_ko, text(joint.name, `관절 ${index + 1}`)));
    option.value = String(index);
    jointSelect.append(option);
  });
  jointSelectLabel.append(jointSelect);
  graphHead.append(graphTitle, jointSelectLabel);
  graph.append(graphHead);
  const legend = el("div", "inspection-legend");
  legend.append(el("span", "inspection-key is-command", "목표 명령"));
  legend.append(el("span", "inspection-key is-observation", "저장된 상태"));
  graph.append(legend);
  const graphHint = el("p", "inspection-graph-hint", "그래프를 누르거나 좌우 방향키로 시간을 이동할 수 있습니다.");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("inspection-chart");
  svg.setAttribute("viewBox", "0 0 900 270");
  svg.setAttribute("role", "slider");
  svg.setAttribute("tabindex", "0");
  svg.setAttribute("aria-label", "시간에 따른 목표 명령과 저장된 상태 그래프");
  svg.setAttribute("aria-valuemin", "0");
  svg.setAttribute("aria-valuemax", slider.max);
  svg.setAttribute("aria-valuenow", "0");
  graph.append(svg, graphHint);
  evidence.append(graph);
  workspace.append(evidence);

  const findings = el("aside", "inspection-findings");
  findings.setAttribute("aria-labelledby", "findings-title");
  const findingsTitle = el("h2", "", issues.length ? `확인할 관절·기록 구간 ${issues.length}개` : "검사 결과");
  findingsTitle.id = "findings-title";
  findings.append(findingsTitle);
  const findingsLead = el("p", "inspection-findings-lead", issues.length
    ? "구간을 선택하면 영상과 그래프가 해당 시각으로 이동합니다."
    : "현재 설정으로 찾은 경고 구간이 없습니다.");
  findings.append(findingsLead);

  const issueList = el("ol", "inspection-issue-list");
  const jointIndexByName = new Map(joints.map((joint, index) => [joint.name, index]));
  issues.forEach((issue, index) => {
    const item = el("li", "inspection-issue");
    const jointIndex = jointIndexByName.get(issue.joint);
    const joint = jointIndex === undefined ? null : joints[jointIndex];
    const label = joint ? text(joint.label_ko, issue.joint) : issue.joint === "timestamp" ? "기록 시간" : text(issue.joint, "관절");
    const title = issue.kind === "sampling_gap"
      ? `${seconds(elapsed(issue.t_start_s))} 부근: 기록 간격이 벌어졌습니다`
      : issue.kind === "source_identity_stagnation"
        ? `${number(elapsed(issue.t_start_s), 2)}~${number(elapsed(issue.t_end_s), 2)}초: 센서 측정 식별자가 바뀌지 않았습니다`
        : `${label} ${number(elapsed(issue.t_start_s), 2)}~${number(elapsed(issue.t_end_s), 2)}초`;
    const unitLabel = issue.unit === "deg" ? "도" : issue.unit === "s" ? "초" : issue.unit === "lerobot_raw" ? "단위" : text(issue.unit, "");
    item.append(el("h3", "", title));
    if (issue.kind === "sampling_gap") {
      item.append(el("p", "inspection-issue-evidence", `샘플 사이 간격 ${seconds(issue.evidence_duration_s)} · 기록 연속성 확인 필요`));
    } else if (issue.kind === "source_identity_stagnation") {
      item.append(el("p", "inspection-issue-evidence", `${seconds(issue.evidence_duration_s)} 동안 같은 측정 식별자 반복`));
      item.append(el("p", "inspection-issue-verdict", "센서 상태 갱신 정체 의심 · 수집기 확인 필요"));
    } else {
      item.append(el("p", "inspection-issue-evidence", `명령 ${number(issue.command_span, 2)}${unitLabel} 변화 · 기록 ${number(issue.observation_span, 2)}${unitLabel} 변화`));
      item.append(el("p", "inspection-issue-verdict", "관절 상태 정체 의심 · 원인 확인 필요"));
    }
    item.append(el("p", "inspection-issue-reason", text(issue.reason_ko)));
    const button = el("button", "inspection-jump", "이 구간 보기");
    button.type = "button";
    button.addEventListener("click", () => {
      if (jointIndex !== undefined) jointSelect.value = String(jointIndex);
      selectedIssue = index;
      renderGraph();
      seek(elapsed(issue.t_start_s) || 0, true);
      evidence.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    });
    item.append(button);
    issueList.append(item);
  });
  findings.append(issueList);

  const coverage = check.coverage && typeof check.coverage === "object" ? check.coverage : {};
  const reproducibility = payload.reproducibility && typeof payload.reproducibility === "object" ? payload.reproducibility : {};
  const details = el("details", "inspection-details");
  details.append(el("summary", "", "검사 범위와 한계 보기"));
  const detailsBody = el("div", "inspection-details-body");
  const detailList = el("dl", "inspection-detail-list");
  const addDetail = (term, value) => {
    detailList.append(el("dt", "", term), el("dd", "", value));
  };
  addDetail("출처", text(source.label, "수집 기록"));
  addDetail("파일 지문", text(source.sha256));
  addDetail("검사기", text(reproducibility.detector, text(check.detector)));
  addDetail("현업 검증", reproducibility.field_validated === true ? "검증됨" : "아직 검증되지 않은 제안 설정");
  addDetail("검사한 관절", Array.isArray(coverage.checked_joints) && coverage.checked_joints.length ? coverage.checked_joints.join(", ") : "확인할 수 없음");
  addDetail("그리퍼", coverage.gripper_checked ? "검사함" : text(coverage.gripper_not_checked_reason, "검사하지 않음"));
  const freshness = coverage.freshness && typeof coverage.freshness === "object" ? coverage.freshness : {};
  addDetail("센서 갱신 여부", text(freshness.reason_ko, text(coverage.observation_freshness_reason_ko, "원본 센서 시각과 순서 번호가 없어 판단할 수 없습니다.")));
  addDetail("원인", check.cause_status === "determined" ? "확인됨" : "확정하지 않음");
  addDetail("시간 기준", `기록 시작부터 경과한 시간입니다. 원본 시작 시각은 ${seconds(timeOrigin)}입니다.`);
  addDetail("센서 분해능", "알려진 관절 분해능을 설정하면 명령 변화 기준에 반영합니다. 분해능이 없으면 정상적인 계단형 측정값도 경고할 수 있습니다.");
  detailsBody.append(detailList);
  const parameters = reproducibility.parameters || check.parameters;
  if (parameters && typeof parameters === "object") {
    const pre = el("pre", "inspection-parameters");
    pre.textContent = JSON.stringify(parameters, null, 2);
    detailsBody.append(el("h3", "", "검사 설정"), pre);
  }
  const nextChecks = Array.isArray(check.next_checks_ko) ? check.next_checks_ko : [];
  if (nextChecks.length) {
    detailsBody.append(el("h3", "", "다음 확인"));
    const list = el("ul", "inspection-next-list");
    nextChecks.forEach((item) => list.append(el("li", "", text(item))));
    detailsBody.append(list);
  }
  details.append(detailsBody);
  findings.append(details);
  workspace.append(findings);
  app.append(workspace);

  let selectedIssue = issues.length ? 0 : -1;
  let currentTime = 0;
  let animationFrame = 0;

  const master = videos[0] || videos[1] || null;
  const duration = () => {
    const reported = Number(summary.duration_s);
    if (Number.isFinite(reported) && reported > 0) return reported;
    if (times.length && Number.isFinite(times[times.length - 1])) return times[times.length - 1];
    return master && Number.isFinite(master.duration) ? master.duration : 0;
  };

  function syncVideos(time, force = false) {
    videos.forEach((video) => {
      if (force || Math.abs(video.currentTime - time) > 0.12) video.currentTime = Math.min(time, Number.isFinite(video.duration) ? video.duration : time);
    });
  }

  function updateTime(time) {
    currentTime = Math.max(0, Math.min(duration() || 0, Number(time) || 0));
    slider.max = String(duration());
    slider.value = String(currentTime);
    timeOutput.textContent = `${number(currentTime, 1)}초 / ${number(duration(), 1)}초`;
    svg.setAttribute("aria-valuemax", String(duration()));
    svg.setAttribute("aria-valuenow", String(currentTime));
    const cursor = svg.querySelector(".inspection-cursor");
    if (cursor) {
      const x = 54 + (duration() ? currentTime / duration() : 0) * 826;
      cursor.setAttribute("x1", String(x));
      cursor.setAttribute("x2", String(x));
    }
  }

  function seek(time, pause = false) {
    if (pause) pauseAll();
    updateTime(time);
    syncVideos(currentTime, true);
  }

  function pauseAll() {
    videos.forEach((video) => video.pause());
    cancelAnimationFrame(animationFrame);
    playButton.textContent = "재생";
    playButton.setAttribute("aria-pressed", "false");
  }

  async function playAll() {
    if (!videos.length) return;
    try {
      await Promise.all(videos.map((video) => video.play()));
      playButton.textContent = "일시정지";
      playButton.setAttribute("aria-pressed", "true");
      tick();
    } catch (_error) {
      pauseAll();
    }
  }

  function tick() {
    if (!master || master.paused) return;
    updateTime(master.currentTime);
    videos.slice(1).forEach((video) => {
      if (Math.abs(video.currentTime - master.currentTime) > 0.12) video.currentTime = master.currentTime;
    });
    animationFrame = requestAnimationFrame(tick);
  }

  function pathFor(values, xMin, xMax, yMin, yMax) {
    const count = Math.min(times.length, Array.isArray(values) ? values.length : 0);
    if (!count) return "";
    const width = 826;
    const height = 196;
    let drawing = false;
    return Array.from({ length: count }, (_, index) => {
      const value = Number(values[index]);
      const time = Number(times[index]);
      if (values[index] === null || values[index] === undefined || !Number.isFinite(value) || !Number.isFinite(time)) {
        drawing = false;
        return "";
      }
      const x = 54 + ((time - xMin) / Math.max(0.001, xMax - xMin)) * width;
      const y = 20 + (1 - (value - yMin) / Math.max(0.001, yMax - yMin)) * height;
      const command = drawing ? "L" : "M";
      drawing = true;
      return `${command}${x.toFixed(2)},${y.toFixed(2)}`;
    }).join(" ");
  }

  function svgNode(name, attributes = {}) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    return node;
  }

  function renderGraph() {
    svg.replaceChildren();
    if (!joints.length || !times.length) {
      const emptyText = svgNode("text", { x: 450, y: 138, "text-anchor": "middle", class: "inspection-axis-label" });
      emptyText.textContent = "표시할 관절 시계열이 없습니다.";
      svg.append(emptyText);
      return;
    }
    const selected = joints[Math.max(0, Number(jointSelect.value) || 0)];
    const cleanValues = (values) => Array.isArray(values)
      ? values.map((value) => value === null || value === undefined ? Number.NaN : Number(value))
      : [];
    const command = cleanValues(selected.command);
    const observation = cleanValues(selected.observation);
    const finiteValues = [...command, ...observation].filter(Number.isFinite);
    const rawMin = finiteValues.length ? Math.min(...finiteValues) : 0;
    const rawMax = finiteValues.length ? Math.max(...finiteValues) : 1;
    const padding = Math.max(0.5, (rawMax - rawMin) * 0.08);
    const yMin = rawMin - padding;
    const yMax = rawMax + padding;
    const xMin = Number(times[0]) || 0;
    const xMax = Number(times[times.length - 1]) || duration() || 1;

    const grid = svgNode("g", { class: "inspection-grid" });
    for (let index = 0; index <= 4; index += 1) {
      const y = 20 + index * 49;
      grid.append(svgNode("line", { x1: 54, x2: 880, y1: y, y2: y }));
      const label = svgNode("text", { x: 46, y: y + 4, "text-anchor": "end" });
      label.textContent = number(yMax - ((yMax - yMin) * index) / 4, 1);
      grid.append(label);
    }
    svg.append(grid);

    const selectedJoint = selected.name;
    issues.forEach((issue, index) => {
      if (issue.joint !== selectedJoint) return;
      const x1 = 54 + ((Number(issue.t_start_s) - xMin) / Math.max(0.001, xMax - xMin)) * 826;
      const x2 = 54 + ((Number(issue.t_end_s) - xMin) / Math.max(0.001, xMax - xMin)) * 826;
      svg.append(svgNode("rect", {
        class: index === selectedIssue ? "inspection-issue-band is-selected" : "inspection-issue-band",
        x: x1,
        y: 20,
        width: Math.max(2, x2 - x1),
        height: 196,
      }));
    });
    svg.append(svgNode("path", { class: "inspection-line is-command", d: pathFor(command, xMin, xMax, yMin, yMax) }));
    svg.append(svgNode("path", { class: "inspection-line is-observation", d: pathFor(observation, xMin, xMax, yMin, yMax) }));
    svg.append(svgNode("line", { class: "inspection-cursor", x1: 54, x2: 54, y1: 16, y2: 224 }));
    const xLabel = svgNode("text", { class: "inspection-axis-label", x: 880, y: 252, "text-anchor": "end" });
    xLabel.textContent = `시간(초) · ${selected.unit === "deg" ? "각도(도)" : text(selected.unit, "값")}`;
    svg.append(xLabel);
    updateTime(currentTime);
  }

  playButton.disabled = videos.length === 0;
  playButton.addEventListener("click", () => {
    if (!master || master.paused) playAll(); else pauseAll();
  });
  slider.addEventListener("input", () => seek(Number(slider.value), true));
  jointSelect.addEventListener("change", () => {
    selectedIssue = -1;
    renderGraph();
  });
  svg.addEventListener("click", (event) => {
    const rect = svg.getBoundingClientRect();
    const plotLeft = rect.left + (54 / 900) * rect.width;
    const plotWidth = (826 / 900) * rect.width;
    const ratio = Math.max(0, Math.min(1, (event.clientX - plotLeft) / plotWidth));
    seek(ratio * duration(), true);
  });
  svg.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    seek(currentTime + (event.key === "ArrowRight" ? 0.1 : -0.1), true);
  });
  videos.forEach((video) => {
    video.addEventListener("ended", pauseAll);
    video.addEventListener("seeking", () => {
      if (video === master) updateTime(video.currentTime);
    });
  });

  if (issues.length) {
    const firstJoint = jointIndexByName.get(issues[0].joint);
    if (firstJoint !== undefined) jointSelect.value = String(firstJoint);
  }
  renderGraph();
  if (videos.length && !reducedMotion) playAll();
})();
