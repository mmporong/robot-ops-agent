/* robot-ops 사고 재생 리포트 — 정적 단일 페이지(해시 라우트), 외부 요청 없음. */
(function () {
  "use strict";

  var SVGNS = "http://www.w3.org/2000/svg";
  var ORIGIN = { real_recording: "실측 기록", sim_run: "시뮬 실행 기록", fault_injection: "오류 주입(시뮬)" };
  var BASIS = {
    rigid_proxy_lift: "강체 패드 접촉 시뮬 기준",
    oracle_recovery_sim_coordinates: "정답 좌표 복구 조건",
    kinematic_drive: "차동 구동 운동학 모델",
    isaac_drive: "물리 시뮬 주행",
    physical: "실물"
  };
  var ASSEMBLY = { left_arm_proxy: "왼팔 단독", bimanual_full: "양팔 전체", single_arm_so101: "SO-101 단일 팔" };
  var LAYER = {
    ik_reachability: "IK 도달",
    control_runtime: "제어 런타임",
    navigation: "주행 제어",
    hardware: "하드웨어",
    perception: "인식",
    planning: "계획"
  };
  var STATUS = {
    active: "활성",
    stale: "과거 기록(현재 코드에서 사라짐)",
    not_reproducible_in_model: "모델에서 미재현"
  };
  var TASK = { lerobot_episode: "기록 재생", cup_contact: "컵 접촉 격자", drive_kinematic: "주행 운동학 격자" };
  var JOINT = {
    shoulder_pan: "어깨 회전",
    shoulder_lift: "어깨 들기",
    elbow_flex: "팔꿈치",
    wrist_flex: "손목 굽힘",
    wrist_roll: "손목 회전"
  };
  var CELL = { pass: "통과", fail: "실패", infra: "infra" };
  var FAILURE = {
    premature_cup_contact: "조기 접촉",
    cup_displaced_before_close: "닫기 전 컵 이동",
    not_arrived_within_horizon: "시간 내 미도착",
    reversal_rate_exceeded: "반전율 초과"
  };
  // 컵 접촉 시뮬 단계(팀 plan.json 단계 이름). 목표 자세 = 컵 중심 기준 오프셋.
  var PHASE = {
    RESET: "시작 자세",
    REORIENT_ABOVE: "컵 위로 자세 맞춤",
    PREGRASP_ABOVE: "컵 위 대기",
    ALIGN_MIDDLE: "컵 중간 높이로 하강",
    APPROACH: "컵 쪽으로 전진",
    CLOSE: "죠 닫기",
    CONTACT_HOLD: "접촉 유지",
    LIFT: "들어 올리기",
    LIFT_HOLD: "든 채 유지",
    RETREAT: "후퇴",
    REOBSERVE: "다시 관측"
  };
  var PAD = { fixed: "고정 죠 패드", moving: "이동 죠 패드" };
  var PAD_INDEX = { fixed: 0, moving: 1 };
  var EVENT = { preclose_failure: "판정", retreat_started: "후퇴 시작", replanned: "재계획" };
  var SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"];
  var MIRROR_CAPTION = "궤적 모델 = SO-101 MuJoCo 미러(양팔 로봇 왼팔과 같은 SO-101 기구)";
  var GRID_TASKS = { cup_contact: true, drive_kinematic: true };

  var state = { build: null, cards: [], hero: null, outOfScope: [], cache: {}, cleanup: [] };

  // ------------------------------------------------------------ DOM 도우미
  function h(tag, attrs) {
    var el = document.createElement(tag);
    setAttrs(el, attrs);
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function s(tag, attrs) {
    var el = document.createElementNS(SVGNS, tag);
    setAttrs(el, attrs);
    for (var i = 2; i < arguments.length; i++) append(el, arguments[i]);
    return el;
  }
  function setAttrs(el, attrs) {
    if (!attrs) return;
    Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "text") el.textContent = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    });
  }
  function append(el, child) {
    if (child === null || child === undefined || child === false) return;
    if (Array.isArray(child)) return child.forEach(function (c) { append(el, c); });
    el.appendChild(typeof child === "string" || typeof child === "number" ? document.createTextNode(String(child)) : child);
  }
  function fmt(v, digits) {
    if (v === null || v === undefined || isNaN(v)) return "–";
    return Number(v).toLocaleString("ko-KR", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  function mm(v) {
    var n = Number(v);
    return (n > 0 ? "+" : n < 0 ? "−" : "") + Math.abs(n).toLocaleString("ko-KR", { maximumFractionDigits: 2 });
  }
  function getJSON(path) {
    if (state.cache[path]) return Promise.resolve(state.cache[path]);
    return fetch(path, { cache: "no-cache" }).then(function (r) {
      if (!r.ok) throw new Error(path + " " + r.status);
      return r.json();
    }).then(function (doc) { state.cache[path] = doc; return doc; });
  }

  // 받침 유무에 맞춘 조사 '으로/로'(받침 없음·ㄹ받침 → 로)
  function withRo(word) {
    var code = String(word).charCodeAt(String(word).length - 1) - 0xac00;
    if (code < 0 || code > 11171) return word + "(으)로";
    var jong = code % 28;
    return word + (jong === 0 || jong === 8 ? "로" : "으로");
  }
  function shortLabel(label) { return String(label || "").split("(")[0].trim(); }

  function badge(kind, label, value) {
    return h("span", { class: "badge badge-" + kind }, h("span", { class: "badge-key", text: label }), h("span", { class: "badge-val", text: value }));
  }
  function conditionBadges(card) {
    return h("div", { class: "badges" },
      badge("origin", "출처", ORIGIN[card.origin] || card.origin),
      badge("basis", "판정 조건", BASIS[card.judgement_basis] || card.judgement_basis)
    );
  }

  // ------------------------------------------------------------ 히어로
  function heroSummary(hero) {
    var lines = [];
    var cup = hero.cup_grid;
    if (hero.source === "observation_freeze") {
      var m = hero.metrics || {};
      lines.push(h("p", { class: "hero-kicker", text: "영상 = 사례 ① 실측 기록 재생" }));
      lines.push(h("p", { class: "hero-facts" },
        "SO-101 텔레옵 기록에서 명령이 움직이는 동안 관측이 멈춘 최장 구간은 ",
        h("strong", { class: "num", text: fmt(hero.incident.t_start_s, 1) + "–" + fmt(hero.incident.t_end_s, 1) + " s" }),
        "입니다. 명령과 관측의 최대 TCP 거리 ",
        h("strong", { class: "num", text: fmt(m.max_tcp_mm, 0) + " mm" }), "."
      ));
      lines.push(h("div", { class: "badges" },
        badge("origin", "출처", ORIGIN[hero.origin] || hero.origin),
        badge("basis", "판정 조건", BASIS[hero.judgement_basis] || hero.judgement_basis),
        h("a", { class: "text-link", href: "#/s/" + hero.scenario_id + "/" + hero.condition, text: "사고 상세 보기" })
      ));
    }
    if (cup) {
      var grid = cup.grid || {};
      var a = (cup.conditions || {}).A || {}, b = (cup.conditions || {}).B || {};
      if (hero.source === "cup_grid") {
        lines.push(h("p", { class: "hero-kicker", text: "영상 = 사례 ② 시뮬 회귀 평가 · 격자점 x " + mm((hero.point || {}).x_mm) + " mm, y " + mm((hero.point || {}).y_mm) + " mm" }));
      }
      var p = h("p", { class: hero.source === "cup_grid" ? "hero-facts" : "hero-facts hero-facts-minor" });
      append(p, "컵 위치 ±" + fmt(grid.range_mm, 0) + " mm 격자 " + grid.points + "점 · ");
      if (cup.measured && a.valid !== undefined && b.valid !== undefined) {
        append(p, [h("span", { class: "nowrap" }, shortLabel(a.label_ko) + " ", h("strong", { class: "num", text: a.pass + "/" + a.valid })), " → ",
          h("span", { class: "nowrap" }, shortLabel(b.label_ko) + " ", h("strong", { class: "num", text: b.pass + "/" + b.valid }))]);
        lines.push(p);
        var reading = ((cup.readings || {}).A || [])[0];
        if (hero.source === "cup_grid" && reading) lines.push(h("p", { class: "hero-reading", text: reading }));
        if (hero.source === "cup_grid") {
          lines.push(h("ol", { class: "hero-steps" },
            h("li", null, "조건 A " + shortLabel(a.label_ko) + ": 판정 순간까지 재생하고 멈춤(접촉력·컵 이동량 표시)"),
            h("li", null, "같은 격자점 조건 B " + shortLabel(b.label_ko) + ": 재계획 뒤 들어 올림"),
            h("li", null, "통과 지도 A | B와 읽는 법 한 줄")
          ));
        }
      } else {
        append(p, [h("span", { class: "pending", text: "측정 전" }), " 격자 실행 결과가 들어오면 조건별 통과 수를 표시합니다."]);
        lines.push(p);
      }
      lines.push(h("div", { class: "badges" },
        badge("origin", "출처", ORIGIN[cup.origin] || cup.origin),
        badge("basis", "조건 A", BASIS[a.judgement_basis] || a.judgement_basis),
        badge("basis", "조건 B", BASIS[b.judgement_basis] || b.judgement_basis),
        badge("assembly", "구성", ASSEMBLY[cup.assembly] || cup.assembly),
        h("a", { class: "text-link", href: "#/s/" + cup.scenario_id, text: "사례 ② 흐름 보기" }),
        h("a", { class: "text-link", href: "#/eval/" + cup.scenario_id, text: "격자 평가 보기" })
      ));
    }
    return lines;
  }

  function heroMedia(hero) {
    if (!hero.video) return h("div", { class: "hero-media hero-empty" }, h("p", { text: "히어로 영상이 아직 없습니다. 재생 실행 뒤 리포트를 다시 빌드하면 채워집니다." }));
    var label = hero.source === "cup_grid" ? "컵 격자 히어로 영상 재생" : "관측 동결 나란히 영상 재생";
    var fig = h("figure", { class: "hero-media" });
    var btn = h("button", { class: "poster-button", type: "button", "aria-label": label },
      hero.poster ? h("img", { src: hero.poster, alt: "", width: hero.width || 1280, height: hero.height || 480 }) : null,
      h("span", { class: "play-mark", "aria-hidden": "true" })
    );
    btn.addEventListener("click", function () {
      var v = h("video", { controls: true, playsinline: true, preload: "auto", poster: hero.poster || null, src: hero.video, class: "hero-video" });
      fig.replaceChild(v, btn);
      v.play().catch(function () {});
      v.focus();
    });
    append(fig, btn);
    var detail = ((hero.segments || [])[0] || {}).highlight_detail;
    append(fig, h("figcaption", null, hero.source === "cup_grid"
      ? "조건 A 격자점 x " + mm((hero.point || {}).x_mm) + " mm, y " + mm((hero.point || {}).y_mm) + " mm 녹화 → 같은 격자점 조건 B 녹화 → 통과 지도 순서입니다. " +
        (detail ? "접촉 크기(" + detail + ")가 작아 화면에서는 거의 보이지 않으므로 판정 순간에 수치를 글자로 얹었습니다. " : "") +
        "녹화는 컵·그리퍼 주변을 잘라 확대했고 붉은 테두리가 판정 시각입니다."
      : "왼쪽 " + (hero.real_label_ko || "실측 카메라") + ", 오른쪽 고스트 렌더(명령 반투명, 관측 불투명). 붉은 띠가 관측 동결 구간입니다."));
    return fig;
  }

  // ------------------------------------------------------------ 홈(히어로 + 사례 두 가지 + 그 밖의 시나리오)
  function caseCard(c, index) {
    var href = "#/s/" + c.scenario_id;
    var kicker = c.kind === "replay" ? "사례 ① 실측 기록 재생" : "사례 ② 시뮬 회귀 평가";
    var rows;
    if (c.kind === "replay") {
      var mt = c.metrics || {}, inc = c.incident || {};
      rows = [
        ["무엇을", "SO-101 텔레옵 기록에 함께 남은 명령(action)과 관측(observation.state)을 MuJoCo 미러로 다시 재생해 관절 추종 오차와 TCP 거리를 계산했습니다."],
        ["왜", "관측 상태가 멈춘 사이 명령만 계속 움직인 구간이 있으면 그 기록은 학습 데이터로 쓰기 어렵습니다. 그 구간을 시각과 수치로 찾습니다."],
        ["결과", "명령이 움직이는 동안 관측이 멈춘 최장 구간은 " + fmt(inc.t_start_s, 1) + "–" + fmt(inc.t_end_s, 1) + " s(" + (inc.n_frames || "–") + "프레임)이고, 명령과 관측의 TCP 거리는 최대 " + fmt(mt.max_tcp_mm, 0) + " mm까지 벌어졌습니다."]
      ];
    } else {
      var a = (c.conditions || {}).A || {}, b = (c.conditions || {}).B || {};
      rows = [
        ["무엇을", "기록된 컵 조기 접촉 사고를 Isaac 5.1에서 컵 위치 ±" + fmt((c.grid || {}).range_mm, 0) + " mm 격자 " + (c.grid || {}).points + "점 × 조건 2개(" + shortLabel(a.label_ko) + "·" + shortLabel(b.label_ko) + ")로 다시 실행했습니다."],
        ["왜", "그 사고가 컵 위치 오차 몇 mm부터 생기는지, 닫기 전 접촉을 판정한 뒤 정답 좌표로 다시 계획하면 달라지는지 확인하기 위해서입니다."],
        ["결과", c.measured ? shortLabel(a.label_ko) + " " + a.pass + "/" + a.valid + " → " + shortLabel(b.label_ko) + " " + b.pass + "/" + b.valid + "." + (c.readings && c.readings[0] ? " " + c.readings[0] + "." : "") : "격자 실행 전"]
      ];
    }
    return h("article", { class: "case case-kind-" + c.kind, "aria-labelledby": "case-" + index },
      h("p", { class: "case-kicker", text: kicker }),
      h("h3", { id: "case-" + index }, h("a", { href: href, text: c.title_ko })),
      h("div", { class: "badges" },
        badge("origin", "출처", ORIGIN[c.origin] || c.origin),
        badge("basis", "판정 조건", BASIS[c.judgement_basis] || c.judgement_basis),
        badge("assembly", "구성", ASSEMBLY[c.assembly] || c.assembly)),
      h("dl", { class: "case-rows" }, rows.map(function (r) { return h("div", null, h("dt", { text: r[0] }), h("dd", { text: r[1] })); })),
      h("p", { class: "case-links" },
        h("a", { class: "text-link", href: href, text: c.kind === "replay" ? "사례 ① 자세히" : "사례 ② 흐름 보기" }),
        c.kind === "grid" ? h("a", { class: "text-link", href: "#/eval/" + c.scenario_id, text: "격자 평가" }) : null)
    );
  }

  function renderHome(main) {
    var hero = state.hero || {};
    main.appendChild(h("section", { class: "hero", "aria-labelledby": "hero-title" },
      h("div", { class: "hero-head" },
        h("h1", { id: "hero-title", tabindex: "-1", text: "명령과 관측이 어긋난 순간을 다시 재생합니다" }),
        h("p", { class: "lead", text: "실행 기록을 다시 읽어 어긋남이 시작된 시각을 수치와 영상으로 보여 주고, 재현한 사고는 같은 조건의 격자로 다시 평가합니다. 모든 결과에 출처와 판정 조건을 붙였습니다." })
      ),
      heroMedia(hero),
      h("div", { class: "hero-summary" }, heroSummary(hero))
    ));

    var cases = hero.cases || [];
    var inCases = {};
    cases.forEach(function (c) { inCases[c.scenario_id] = true; });
    if (cases.length) {
      main.appendChild(h("section", { class: "cases", "aria-labelledby": "cases-title" },
        h("div", { class: "section-head" }, h("h2", { id: "cases-title", text: "사례 " + (cases.length === 2 ? "두 가지" : cases.length + "건") })),
        cases.length === 2 ? h("p", { class: "note cases-note", text: "두 사례는 서로 다른 로봇·기록입니다. ①은 " + (ASSEMBLY[cases[0].assembly] || cases[0].assembly) + "의 " + (ORIGIN[cases[0].origin] || cases[0].origin) + ", ②는 양팔 로봇 " + (ASSEMBLY[cases[1].assembly] || cases[1].assembly) + "의 " + (ORIGIN[cases[1].origin] || cases[1].origin) + "입니다. 공통점은 실행 기록을 다시 읽어 판정 근거를 수치로 남기는 방법입니다." }) : null,
        h("div", { class: "case-grid" }, cases.map(caseCard))
      ));
    }

    var rest = state.cards.filter(function (card) { return !inCases[card.scenario_id]; });
    var list = h("ol", { class: "incident-list" });
    rest.forEach(function (card) {
      var href = "#/s/" + card.scenario_id;
      list.appendChild(h("li", { class: "incident" },
        h("a", { class: "incident-thumb", href: href, tabindex: "-1", "aria-hidden": "true" },
          card.poster ? h("img", { src: card.poster, alt: "", loading: "lazy", width: 320, height: 240 }) : h("span", { class: "thumb-empty", text: TASK[card.task] || card.task })
        ),
        h("div", { class: "incident-body" },
          h("h3", null, h("a", { href: href, text: card.title_ko })),
          conditionBadges(card),
          h("dl", { class: "facts" },
            h("div", null, h("dt", { text: "계층" }), h("dd", { text: LAYER[card.replayable && card.replayable.layer] || (card.replayable && card.replayable.layer) || "–" })),
            h("div", null, h("dt", { text: "상태" }), h("dd", { text: card.status_ko || STATUS[card.status] || card.status })),
            h("div", null, h("dt", { text: "구성" }), h("dd", { text: ASSEMBLY[card.assembly] || card.assembly })),
            h("div", null, h("dt", { text: "과제" }), h("dd", { text: TASK[card.task] || card.task }))
          )
        )
      ));
    });
    var oos = state.outOfScope || [];
    var showList = rest.length || !cases.length;
    main.appendChild(h("section", { class: showList ? "incidents" : "incidents incidents-min", "aria-labelledby": showList ? "list-title" : null },
      showList ? h("div", { class: "section-head" },
        h("h2", { id: "list-title", text: cases.length ? "그 밖의 시나리오" : "사고 목록" }),
        h("p", { class: "count num", text: rest.length + "건" })
      ) : null,
      showList ? (rest.length ? list : h("p", { class: "empty", text: "이 빌드에 포함된 시나리오가 없습니다." })) : null,
      h("details", { class: "out-of-scope" },
        h("summary", null, "재생 대상 외 사고 " + oos.length + "건(전기·펌웨어·기록 없음)"),
        h("p", { class: "note", text: "궤적 기록이 없거나 원인이 전기·펌웨어 계층이라 명령·관측 재생으로 다룰 수 없는 사고입니다." }),
        h("ul", null, oos.map(function (o) { return h("li", null, o.title_ko, h("span", { class: "muted", text: "  " + o.reason_ko })); }))
      )
    ));
  }

  // ------------------------------------------------------------ 추종 오차 그래프(SVG)
  function niceMax(v) {
    if (!(v > 0)) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v)));
    var steps = [1, 2, 2.5, 5, 10];
    for (var i = 0; i < steps.length; i++) if (steps[i] * p >= v) return steps[i] * p;
    return 10 * p;
  }

  function Graph(host, view, onSeek) {
    this.host = host;
    this.view = view;
    this.onSeek = onSeek;
    this.t = 0;
    this.render();
  }
  Graph.prototype.render = function () {
    var view = this.view, host = this.host, self = this;
    host.textContent = "";
    var W = Math.max(280, Math.round(host.clientWidth || 800));
    var compact = W < 600;
    var H = compact ? 160 : 300;
    var m = { l: compact ? 34 : 46, r: compact ? 8 : 14, t: 16, b: 22 };
    var gap = compact ? 18 : 22;
    var ph = H - m.t - m.b - gap;
    var h1 = Math.round(ph * 0.6), h2 = ph - h1;
    var y1top = m.t, y2top = m.t + h1 + gap;
    var ser = view.series, T = ser.t;
    var t0 = 0, t1 = view.duration_s || (T.length ? T[T.length - 1] : 1);
    var px0 = m.l, px1 = W - m.r;
    function X(t) { return px0 + (t - t0) / (t1 - t0) * (px1 - px0); }
    var jmax = 0, tmax = 0;
    ser.joint_err_deg.forEach(function (row) { row.forEach(function (v) { jmax = Math.max(jmax, Math.abs(v)); }); });
    ser.tcp_mm.forEach(function (v) { if (v !== null) tmax = Math.max(tmax, v); });
    var th = view.thresholds || {};
    jmax = niceMax(Math.max(jmax, th.joint_deg || 0));
    tmax = niceMax(Math.max(tmax, th.tcp_mm || 0));
    function Y1(v) { return y1top + h1 - Math.abs(v) / jmax * h1; }
    function Y2(v) { return y2top + h2 - v / tmax * h2; }
    this.X = X;
    this.t0 = t0; this.t1 = t1; this.px0 = px0; this.px1 = px1;

    var svg = s("svg", {
      viewBox: "0 0 " + W + " " + H, width: W, height: H, class: "graph-svg", role: "img",
      "aria-label": "관절 추종 오차와 TCP 거리 시계열. 붉은 띠는 관측 동결 구간입니다.",
      "data-t0": t0, "data-t1": t1, "data-px0": px0, "data-px1": px1
    });
    // 사고 띠(모든 동결은 옅게, 대표 구간은 진하게)
    (view.freezes || []).forEach(function (f) {
      svg.appendChild(s("rect", { x: X(f.t_start_s), y: m.t, width: Math.max(1, X(f.t_end_s) - X(f.t_start_s)), height: H - m.t - m.b, class: "band-minor" }));
    });
    var inc = view.incident || {};
    svg.appendChild(s("rect", { x: X(inc.t_start_s), y: m.t - 6, width: Math.max(2, X(inc.t_end_s) - X(inc.t_start_s)), height: H - m.t - m.b + 6, class: "band" }));
    // 축·격자
    [[y1top, h1, jmax, "°", Y1], [y2top, h2, tmax, " mm", Y2]].forEach(function (p) {
      var ticks = [0, p[2] / 2, p[2]];
      ticks.forEach(function (v) {
        var y = p[4](v);
        svg.appendChild(s("line", { x1: px0, x2: px1, y1: y, y2: y, class: v === 0 ? "axis" : "grid" }));
        svg.appendChild(s("text", { x: px0 - 6, y: y + 4, class: "tick", "text-anchor": "end", text: fmt(v, 0) }));
      });
    });
    var step = niceMax((t1 - t0) / (compact ? 4 : 8));
    for (var tt = 0; tt <= t1 + 1e-6; tt += step) {
      svg.appendChild(s("text", { x: X(tt), y: H - 6, class: "tick", "text-anchor": "middle", text: fmt(tt, 0) + (tt + step > t1 ? " s" : "") }));
    }
    // 임계선
    if (th.joint_deg) {
      svg.appendChild(s("line", { x1: px0, x2: px1, y1: Y1(th.joint_deg), y2: Y1(th.joint_deg), class: "threshold" }));
    }
    if (th.tcp_mm) {
      svg.appendChild(s("line", { x1: px0, x2: px1, y1: Y2(th.tcp_mm), y2: Y2(th.tcp_mm), class: "threshold" }));
    }
    svg.appendChild(s("text", { x: px0 + 4, y: y1top + 10, class: "panel-label", text: "관절 추종 오차 (°)" }));
    svg.appendChild(s("text", { x: px0 + 4, y: y2top + 10, class: "panel-label", text: "TCP 거리 (mm)" }));
    // 선
    var joints = view.joint_names || [];
    joints.forEach(function (name, j) {
      var d = "";
      T.forEach(function (t, i) { d += (i ? "L" : "M") + X(t).toFixed(1) + " " + Y1(ser.joint_err_deg[i][j]).toFixed(1); });
      svg.appendChild(s("path", { d: d, class: "line joint", stroke: SERIES[j % SERIES.length], "data-joint": name }));
    });
    var dt = "";
    var pen = false;
    T.forEach(function (t, i) {
      var v = ser.tcp_mm[i];
      if (v === null || v === undefined) { pen = false; return; }
      dt += (pen ? "L" : "M") + X(t).toFixed(1) + " " + Y2(v).toFixed(1);
      pen = true;
    });
    svg.appendChild(s("path", { d: dt, class: "line tcp" }));
    // 호버 십자선 + 재생 헤드
    var cross = s("line", { x1: -10, x2: -10, y1: m.t, y2: H - m.b, class: "crosshair" });
    svg.appendChild(cross);
    this.playhead = s("line", { x1: X(this.t), x2: X(this.t), y1: m.t - 8, y2: H - m.b, class: "playhead", id: "playhead" });
    svg.appendChild(this.playhead);
    this.knob = s("circle", { cx: X(this.t), cy: m.t - 8, r: 4, class: "playhead-knob" });
    svg.appendChild(this.knob);
    var hit = s("rect", { x: px0, y: 0, width: px1 - px0, height: H, class: "hit" });
    svg.appendChild(hit);
    host.appendChild(svg);
    var tip = h("div", { class: "tooltip", role: "status", "aria-live": "off", hidden: true });
    host.appendChild(tip);

    function tAt(evt) {
      var r = svg.getBoundingClientRect();
      var x = (evt.clientX - r.left) * (W / r.width);
      return Math.min(t1, Math.max(t0, t0 + (x - px0) / (px1 - px0) * (t1 - t0)));
    }
    function idxAt(t) {
      var best = 0;
      for (var i = 0; i < T.length; i++) if (Math.abs(T[i] - t) < Math.abs(T[best] - t)) best = i;
      return best;
    }
    hit.addEventListener("pointermove", function (evt) {
      var t = tAt(evt), i = idxAt(t);
      var x = X(T[i]);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x);
      tip.hidden = false;
      tip.textContent = "";
      append(tip, h("strong", { class: "num", text: fmt(T[i], 1) + " s" }));
      joints.forEach(function (name, j) {
        append(tip, h("span", { class: "tip-row" }, h("i", { class: "sw sw-" + j }), (JOINT[name] || name) + " ", h("b", { class: "num", text: fmt(ser.joint_err_deg[i][j], 1) + "°" })));
      });
      append(tip, h("span", { class: "tip-row" }, h("i", { class: "sw sw-tcp" }), "TCP 거리 ", h("b", { class: "num", text: fmt(ser.tcp_mm[i], 0) + " mm" })));
      var r = svg.getBoundingClientRect();
      var left = x * (r.width / W);
      tip.classList.toggle("tip-left", left > r.width * 0.6);
      tip.style.left = left + "px";
    });
    hit.addEventListener("pointerleave", function () { tip.hidden = true; cross.setAttribute("x1", -10); cross.setAttribute("x2", -10); });
    hit.addEventListener("click", function (evt) { self.onSeek(tAt(evt)); });
    this.setTime(this.t);
  };
  Graph.prototype.setTime = function (t) {
    this.t = t;
    if (!this.playhead) return;
    var x = this.X(Math.min(this.t1, Math.max(this.t0, t)));
    this.playhead.setAttribute("x1", x.toFixed(2));
    this.playhead.setAttribute("x2", x.toFixed(2));
    this.knob.setAttribute("cx", x.toFixed(2));
  };

  // ------------------------------------------------------------ 영상 동기화
  // Range 요청을 지원하지 않는 서버(python -m http.server)에서는 받아 둔 구간만 seek된다.
  // 목표 시각이 seekable 범위에 들어올 때까지 받아 두었다가 이동한다(마지막 요청만 유효).
  function inSeekable(v, t) {
    for (var i = 0; i < v.seekable.length; i++) {
      if (t >= v.seekable.start(i) - 1e-3 && t <= v.seekable.end(i) + 1e-3) return true;
    }
    return false;
  }
  function seekVideo(v, t) {
    v._seekTarget = t;
    if (v.readyState > 0 && inSeekable(v, t)) { v.currentTime = t; return; }
    if (v.preload !== "auto") v.preload = "auto";
    if (v.readyState === 0 && v.networkState !== 2) v.load();
    if (v._seekTimer) return;
    var tries = 0;
    v._seekTimer = setInterval(function () {
      var target = v._seekTarget;
      if ((v.readyState > 0 && inSeekable(v, target)) || ++tries > 300) {
        clearInterval(v._seekTimer);
        v._seekTimer = 0;
        if (v.readyState > 0) v.currentTime = target;
      }
    }, 50);
  }

  function syncVideos(vids, onTime) {
    vids = vids.filter(Boolean);
    var raf = 0;
    function others(v) { return vids.filter(function (o) { return o !== v; }); }
    function tick() {
      var lead = vids[0];
      onTime(lead.currentTime);
      others(lead).forEach(function (o) {
        if (!o.paused && Math.abs(o.currentTime - lead.currentTime) > 0.15) seekVideo(o, lead.currentTime);
      });
      raf = lead.paused ? 0 : requestAnimationFrame(tick);
    }
    vids.forEach(function (v) {
      v.addEventListener("play", function () {
        others(v).forEach(function (o) {
          if (Math.abs(o.currentTime - v.currentTime) > 0.04) seekVideo(o, v.currentTime);
          if (o.paused) o.play().catch(function () {});
        });
        if (!raf) raf = requestAnimationFrame(tick);
      });
      v.addEventListener("pause", function () {
        others(v).forEach(function (o) { if (!o.paused) o.pause(); });
        onTime(v.currentTime);
      });
      ["seeking", "seeked"].forEach(function (name) {
        v.addEventListener(name, function () {
          if (v._seekTimer) return; // 받는 중인 영상의 중간 위치는 전파하지 않는다
          others(v).forEach(function (o) { if (Math.abs(o.currentTime - v.currentTime) > 0.04) seekVideo(o, v.currentTime); });
          onTime(v.currentTime);
        });
      });
      v.addEventListener("timeupdate", function () { if (!raf && !v._seekTimer) onTime(v.currentTime); });
    });
    return {
      seek: function (t) {
        vids.forEach(function (v) { seekVideo(v, t); });
        onTime(t);
      },
      stop: function () { if (raf) cancelAnimationFrame(raf); vids.forEach(function (v) { v.pause(); }); }
    };
  }

  function videoFigure(src, poster, label, caption, cls) {
    return h("figure", { class: "pane " + (cls || "") },
      h("div", { class: "pane-label" }, h("span", { class: "pane-dot " + (cls || ""), "aria-hidden": "true" }), label),
      src ? h("video", { src: src, poster: poster || null, controls: true, playsinline: true, preload: "none", muted: true, "aria-label": label })
        : h("div", { class: "pane-empty", text: "영상 없음" }),
      caption ? h("figcaption", { text: caption }) : null
    );
  }

  // ------------------------------------------------------------ 상세
  function renderDetail(main, sid, condName) {
    return getJSON("data/scenario/" + encodeURIComponent(sid) + ".json").then(function (doc) {
      var views = doc.views || [];
      var view = views.filter(function (v) { return v.condition === condName; })[0] || views[0];
      main.appendChild(h("p", { class: "crumb" }, h("a", { href: "#/", text: "사고 목록으로" })));
      var head = h("header", { class: "detail-head" },
        h("h1", { tabindex: "-1", text: doc.title_ko }),
        conditionBadges(doc),
        h("dl", { class: "facts facts-inline" },
          h("div", null, h("dt", { text: "계층" }), h("dd", { text: LAYER[doc.replayable.layer] || doc.replayable.layer })),
          h("div", null, h("dt", { text: "상태" }), h("dd", { text: doc.status_ko || STATUS[doc.status] || doc.status })),
          h("div", null, h("dt", { text: "구성" }), h("dd", { text: ASSEMBLY[doc.assembly] || doc.assembly }))
        ),
        doc.replayable && doc.replayable.reason ? h("p", { class: "reason", text: doc.replayable.reason }) : null
      );
      main.appendChild(head);
      if (views.length > 1) {
        main.appendChild(h("div", { class: "segmented", role: "tablist", "aria-label": "재생 기록 선택" },
          views.map(function (v) {
            var on = v === view;
            return h("a", { role: "tab", "aria-selected": on ? "true" : "false", class: on ? "seg on" : "seg", href: "#/s/" + sid + "/" + v.condition, text: v.label_ko || v.condition });
          })
        ));
      }
      if (view) renderReplayView(main, doc, view);
      else if (doc.task === "cup_contact" && doc.regress) {
        return getJSON("data/regress/" + encodeURIComponent(sid) + ".json").then(function (reg) {
          if (reg.story) renderCupCase(main, doc, reg);
          else renderGridDetail(main, doc);
          renderSources(main, doc, view);
        });
      } else renderGridDetail(main, doc);
      renderSources(main, doc, view);
    });
  }

  // 사례 ②: 기록된 사고 → 같은 조건 재현 → 주변 25점 → 복구 켬 비교
  function renderCupCase(main, doc, reg) {
    var st = reg.story || {};
    var card = { scenario_id: doc.scenario_id, conditions: doc.conditions, task: doc.task };
    var flow = storyFlow(reg, card, [1, 2]);
    if (flow) main.appendChild(h("section", { class: "flow-block", "aria-label": "사례 흐름" },
      h("p", { class: "flow-kicker", text: "사례 ② 시뮬 회귀 평가 · 네 단계" }), flow));
    var rec = st.recorded, a = (st.cells || {}).A || {}, b = (st.cells || {}).B || {};
    var p = st.point || {};
    var av = a.verdict || {};
    // 1·2 기록된 사고와 같은 조건 재현
    var rows = [];
    function row(label, r, extra) {
      return h("tr", null, h("th", { scope: "row", text: label }),
        h("td", { class: "num", "data-label": "컵 오프셋", text: r ? offsetText(r.offset_mm || [p.x_mm, p.y_mm]) : "–" }),
        h("td", { class: "num", "data-label": "판정 시각", text: r && r.t_s !== undefined ? fmt(r.t_s, 3) + " s" : "–" }),
        h("td", { "data-label": "판정", text: r ? (FAILURE[r.failure_code] || r.failure_code || "–") : "–" }),
        h("td", { class: "num", "data-label": "접촉력", text: r && r.pad ? PAD[r.pad] + " " + f2(Math.max.apply(null, r.force_n)) + " N" : "–" }),
        h("td", { class: "num", "data-label": "컵 이동", text: r && r.cup_disp_mm !== undefined ? f2(r.cup_disp_mm) + " mm" : "–" }),
        extra || null);
    }
    rows.push(row("1 기록" + (rec ? "(" + rec.run_name + ")" : ""), rec));
    rows.push(row("2 재현(조건 A)", av.t_s !== undefined ? Object.assign({ offset_mm: [p.x_mm, p.y_mm] }, av) : null));
    var sec12 = h("section", { class: "case-step", id: "step-2", "aria-labelledby": "step12-title" },
      h("h2", { id: "step12-title" }, h("span", { class: "step-no num", text: "1·2" }), "기록된 사고를 같은 조건으로 다시 실행"),
      h("p", { class: "reason", text: "기록된 사고의 컵 오프셋(" + (rec ? offsetText(rec.offset_mm) : "–") + ")을 조건 A(" + shortLabel(((doc.conditions || {}).A || {}).label_ko) + ")의 격자점으로 다시 실행했습니다. " + (st.same_time ? "판정 시각과 판정 종류가 기록과 같습니다." : "") }),
      h("div", { class: "table-wrap" }, h("table", { class: "table compare-table" },
        h("thead", null, h("tr", null, ["", "컵 오프셋", "판정 시각", "판정", "접촉력", "컵 이동"].map(function (t) { return h("th", { scope: "col", text: t }); }))),
        h("tbody", null, rows))),
      rec ? h("p", { class: "note" }, "기록 원본 ", h("code", { text: rec.path })) : null
    );
    main.appendChild(sec12);
    var evA = h("div", { class: "evidence-host" }, h("p", { class: "note", text: "접촉 증거를 불러오는 중입니다." }));
    sec12.appendChild(evA);
    var linkA = "#/eval/" + doc.scenario_id + "/A/" + p.x_mm + "/" + p.y_mm;
    sec12.appendChild(h("p", null, h("a", { class: "text-link", href: linkA, text: "이 격자점의 검사값·녹화 보기" })));
    // 3·4 격자
    var ca = (reg.conditions || {}).A || {}, cb = (reg.conditions || {}).B || {};
    var readings = (ca.readings || []).map(function (r) { return r.text_ko; });
    var sec34 = h("section", { class: "case-step", id: "step-3", "aria-labelledby": "step34-title" },
      h("h2", { id: "step34-title" }, h("span", { class: "step-no num", text: "3·4" }), "주변 25점으로 넓히고 복구를 켜서 비교"),
      h("p", { class: "reason", text: "같은 사고가 어느 범위에서 생기는지 보려고 컵을 격자 위치로 옮겨 칸마다 한 번씩 실행했습니다. 조건 A " + shortLabel(((doc.conditions || {}).A || {}).label_ko) + " " + ca.pass + "/" + ca.valid + ", 조건 B " + shortLabel(((doc.conditions || {}).B || {}).label_ko) + " " + cb.pass + "/" + cb.valid + "." }),
      readings.length ? h("p", { class: "reading-line" }, readings.map(function (t) { return h("span", { text: t }); })) : null,
      reg.pass_map_webp ? h("figure", { class: "passmap-fig" },
        h("a", { href: "#/eval/" + doc.scenario_id }, h("img", { src: reg.pass_map_webp, alt: "조건 A와 조건 B 통과 지도", width: 1200, height: 600, loading: "lazy" })),
        h("figcaption", null, "칸 하나 = 컵을 그 위치로 옮겨 놓고 Isaac을 한 번 실행한 결과. ", h("a", { class: "text-link", href: "#/eval/" + doc.scenario_id, text: "격자 읽는 법과 칸별 기록 보기" }))) : null
    );
    main.appendChild(sec34);
    var evB = h("div", { class: "evidence-host" });
    if (b.evidence) {
      sec34.appendChild(h("h3", { class: "step-sub", text: "같은 격자점(x " + mm(p.x_mm) + ", y " + mm(p.y_mm) + " mm)에서 조건 B" }));
      sec34.appendChild(evB);
      sec34.appendChild(h("p", null, h("a", { class: "text-link", href: "#/eval/" + doc.scenario_id + "/B/" + p.x_mm + "/" + p.y_mm, text: "조건 B 격자점의 검사값·녹화 보기" })));
    }
    var jobs = [];
    if (a.evidence) jobs.push(getJSON(a.evidence).then(function (ev) { evA.textContent = ""; evA.appendChild(evidenceBlock(ev)); }));
    else evA.textContent = "";
    if (b.evidence) jobs.push(getJSON(b.evidence).then(function (ev) { evB.appendChild(evidenceBlock(ev)); }));
    return Promise.all(jobs);
  }

  function renderReplayView(main, doc, view) {
    var realCap = view.real.upscaled ? "원본 352×288을 높이 480으로 확대했습니다." : null;
    var real = videoFigure(view.real.src, view.real.poster, view.real.label_ko, realCap, "real");
    var fid = view.fidelity || {};
    var ghostCap = MIRROR_CAPTION;
    if (fid.scene_layout === "desk_clamp") ghostCap += ". 장면: 책상 고정" + ((fid.removed_bodies || []).indexOf("mobile_platform") >= 0 ? "(차량 받침대 제거)" : "");
    if (fid.camera_fit) ghostCap += ", 카메라: 실측 3인칭 카메라 시점 근사(" + fid.camera_fit.keypoints + "점 적합" + (fid.camera_fit.rms_px !== undefined && fid.camera_fit.rms_px !== null ? ", RMS " + fmt(fid.camera_fit.rms_px, 1) + " px" : "") + ")";
    var ghost = view.ghost.src
      ? videoFigure(view.ghost.src, view.ghost.poster, view.ghost.label_ko, ghostCap, "ghost")
      : null;
    main.appendChild(h("section", { class: ghost ? "videos" : "videos single", "aria-label": "실측 영상과 고스트 렌더" }, real, ghost));

    var readout = h("output", { class: "readout num", "aria-live": "off", text: "0.0 s" });
    var toIncident = h("button", { type: "button", class: "btn", text: "사고 지점으로" });
    var graphHost = h("div", { class: "graph", id: "graph" });
    var legend = h("ul", { class: "legend", "aria-label": "범례" },
      (view.joint_names || []).map(function (n, j) { return h("li", null, h("i", { class: "sw sw-" + j }), JOINT[n] || n); }),
      h("li", null, h("i", { class: "sw sw-tcp" }), "TCP 거리"),
      h("li", null, h("i", { class: "sw sw-band" }), "관측 동결 구간"),
      h("li", null, h("i", { class: "sw sw-threshold" }), "임계(" + fmt((view.thresholds || {}).joint_deg, 0) + "°, " + fmt((view.thresholds || {}).tcp_mm, 0) + " mm)")
    );
    main.appendChild(h("section", { class: "graph-block", "aria-labelledby": "graph-title" },
      h("div", { class: "graph-head" },
        h("h2", { id: "graph-title", text: "추종 오차" }),
        h("div", { class: "graph-controls" }, readout, toIncident)
      ),
      legend, graphHost,
      h("p", { class: "note", text: "그래프를 누르면 두 영상이 그 시각으로 이동합니다. 교정 차(정적 구간 평균 명령−관측)를 뺀 뒤의 오차입니다." }),
      lagNote(fid.lag)
    ));
    var met = view.metrics || {};
    main.appendChild(h("section", { class: "metrics", "aria-label": "지표" },
      tile(fmt(met.max_tcp_mm, 0), "mm", "최대 TCP 거리", met.tcp_calibration_suspect ? "교정 차가 커서 TCP 지표는 참고용입니다" : "명령 자세와 관측 자세의 TCP 사이 거리, MuJoCo 미러 FK"),
      tile(fmt(met.incident_joint_err_deg, 1), "°", "사고 구간 관절 오차", "구간 안 5관절 중 최대값"),
      tile(fmt(met.incident_t_s, 1), "s", "사고 시각", "관측 동결 " + fmt(view.incident.t_start_s, 1) + "–" + fmt(view.incident.t_end_s, 1) + " s, " + (view.incident.n_frames || "–") + "프레임")
    ));

    var vids = [real.querySelector("video"), ghost && ghost.querySelector("video")].filter(Boolean);
    var graph;
    var sync = syncVideos(vids, function (t) {
      readout.textContent = fmt(t, 1) + " s";
      if (graph) graph.setTime(t);
    });
    graph = new Graph(graphHost, view, function (t) { sync.seek(t); });
    toIncident.addEventListener("click", function () { sync.seek(view.incident.t_start_s); });
    var onResize = debounce(function () { graph.render(); }, 150);
    window.addEventListener("resize", onResize);
    state.cleanup.push(function () { window.removeEventListener("resize", onResize); sync.stop(); });
  }

  // 실측 대조: 명령을 시간 이동해 실측 영상에 겹쳐 본 결과(시각별 지연)
  function lagNote(lag) {
    if (!lag || !lag.lag_range_s || !(lag.lagged_t_s || []).length) return null;
    var lt = lag.lagged_t_s, r = lag.lag_range_s;
    var span = fmt(lt[0], 0) + "–" + fmt(lt[lt.length - 1], 0) + " s";
    var range = r[0] === r[1] ? fmt(r[0], 1) + "초" : fmt(r[0], 1) + "–" + fmt(r[1], 1) + "초";
    var aligned = (lag.aligned_t_s || []).map(function (t) { return fmt(t, 0); }).join("·");
    return h("p", { class: "reason lag-note" },
      "실측 대조: 빠르게 뻗는 구간(" + span + ")에서는 실물 팔이 명령보다 " + range + " 늦게 따라갑니다(명령을 시간 이동해 실측 3인칭 영상에 겹쳐 확인" +
      (aligned ? ", t = " + aligned + " s는 이동 없이 겹침" : "") + "). 이 구간의 TCP 거리에는 관측 동결과 추종 지연이 함께 들어 있습니다.");
  }

  function tile(value, unit, label, note) {
    return h("div", { class: "tile" },
      h("p", { class: "tile-value num" }, value, h("span", { class: "unit", text: " " + unit })),
      h("p", { class: "tile-label", text: label }),
      note ? h("p", { class: "tile-note", text: note }) : null
    );
  }

  function renderGridDetail(main, doc) {
    var reg = doc.regress;
    var rows = Object.keys(doc.conditions || {}).map(function (name) {
      var c = doc.conditions[name];
      var r = reg && reg[name];
      return h("tr", null,
        h("th", { scope: "row", text: "조건 " + name }),
        h("td", { text: c.label_ko }),
        h("td", { text: BASIS[c.judgement_basis] || c.judgement_basis }),
        h("td", { class: "num" }, r && r.valid !== undefined ? r.pass + "/" + r.valid + (r.invalid ? " (지도 무효)" : "") : h("span", { class: "pending", text: "측정 전" }))
      );
    });
    var note = (doc.provenance || {}).incident_note_ko;
    main.appendChild(h("section", { class: "grid-detail" },
      h("h2", { text: "조건과 결과" }),
      h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "조건" }), h("th", { scope: "col", text: "설정" }), h("th", { scope: "col", text: "판정 조건" }), h("th", { scope: "col", text: "통과" }))),
        h("tbody", null, rows)
      )),
      note ? h("p", { class: "reason", text: note }) : null,
      doc.incident ? h("p", { class: "note", text: "기록된 사고: " + doc.incident.code + ", " + fmt(doc.incident.t_start_s, 3) + " s" }) : null,
      h("p", null, h("a", { class: "text-link", href: "#/eval/" + doc.scenario_id, text: "격자 평가 보기" }))
    ));
  }

  function renderSources(main, doc, view) {
    var body = h("div", { class: "prov" });
    if (view && view.provenance) {
      var p = view.provenance;
      append(body, h("p", null, "실행 폴더 ", h("code", { text: p.run_dir })));
      append(body, h("div", { class: "table-wrap" }, h("table", { class: "table prov-table" },
        h("thead", null, h("tr", null, h("th", { scope: "col", text: "항목" }), h("th", { scope: "col", text: "경로" }), h("th", { scope: "col", text: "SHA-256" }))),
        h("tbody", null, p.items.filter(function (i) { return i.path; }).map(function (i) {
          return h("tr", null, h("td", { text: i.label_ko }), h("td", null, h("code", { text: i.path })), h("td", null, h("code", { class: "sha", text: i.sha256 || "–" })));
        }))
      )));
      var tools = Object.keys(p.tool_sha256 || {});
      if (tools.length) {
        append(body, h("p", { class: "note", text: "궤적 모델 파일 SHA-256" }));
        append(body, h("ul", { class: "sha-list" }, tools.map(function (k) { return h("li", null, h("code", { text: k }), " ", h("code", { class: "sha", text: p.tool_sha256[k] })); })));
      }
      append(body, h("p", { class: "note", text: "재현 명령" }));
      append(body, h("pre", { class: "cmd" }, h("code", { text: p.reproduce.join("\n") })));
    }
    append(body, h("p", { class: "note", text: "시나리오 원천 기록" }));
    append(body, h("ul", { class: "sha-list" }, (doc.sources || []).map(function (src) {
      return h("li", null, h("code", { text: src.path }), " ", h("code", { class: "sha", text: src.sha256 }));
    })));
    main.appendChild(h("details", { class: "sources" }, h("summary", null, "출처·재현 명령 (펼치기)"), body));
  }

  // ------------------------------------------------------------ 컵 접촉 증거(힘 그래프·위에서 본 배치·앞에서 본 확대)
  function f2(v) { return fmt(v, 2); }
  function signed(v, digits) {
    var n = Number(v);
    return (n > 0 ? "+" : n < 0 ? "−" : "") + fmt(Math.abs(n), digits === undefined ? 1 : digits);
  }
  function phaseName(name) { return PHASE[name] || name; }
  function axisWord(axes, axis, sign) {
    if (!axes) return (sign > 0 ? "+" : "−") + axis;
    return "로봇 " + axes[axis + (sign > 0 ? "_plus_ko" : "_minus_ko")] + "(" + (sign > 0 ? "+" : "−") + axis + ")";
  }
  function offsetText(off) {
    return "x " + mm(off[0]) + " mm, y " + mm(off[1]) + " mm";
  }

  function ContactPlot(host, ev, opts) {
    this.host = host;
    this.ev = ev;
    this.opts = opts || {};
    this.render();
  }
  ContactPlot.prototype.render = function () {
    var ev = this.ev, host = this.host;
    host.textContent = "";
    var W = Math.max(280, Math.round(host.clientWidth || 800));
    var compact = W < 600;
    var strip = 26;
    var m = { l: compact ? 38 : 50, r: compact ? 10 : 16, t: strip + 26, b: 24 };
    var h1 = compact ? 118 : 150, gap = 30, h2 = compact ? 56 : 70;
    var H = m.t + h1 + gap + h2 + m.b;
    var T = ev.t || [];
    var tEnd = ev.duration_s || (T.length ? T[T.length - 1] : 1);
    var t1 = tEnd * 1.03; // 판정 순간(끝)의 봉우리와 판정선이 테두리에 붙지 않게 조금 띄운다
    var px0 = m.l, px1 = W - m.r;
    function X(t) { return px0 + t / t1 * (px1 - px0); }
    var f0 = (ev.force_n || [[], []])[0] || [], fm = (ev.force_n || [[], []])[1] || [];
    var fmax = 0, dmax = 0;
    f0.concat(fm).forEach(function (v) { fmax = Math.max(fmax, v); });
    (ev.cup_disp_mm || []).forEach(function (v) { dmax = Math.max(dmax, v); });
    fmax = niceMax(Math.max(fmax, (ev.threshold_n || 0) * 5));
    dmax = niceMax(Math.max(dmax, ev.preclose_disp_mm || 0));
    var y1top = m.t, y2top = m.t + h1 + gap;
    function Y1(v) { return y1top + h1 - v / fmax * h1; }
    function Y2(v) { return y2top + h2 - v / dmax * h2; }
    var svg = s("svg", { viewBox: "0 0 " + W + " " + H, width: W, height: H, class: "graph-svg contact-svg", role: "img",
      "aria-label": "패드 접촉력과 컵 수평 이동 시계열. 위 띠는 동작 단계, 붉은 세로선은 판정 시각입니다." });
    // 단계 띠(위) + 닫기 전 구간 음영(그래프 전체 높이)
    var phases = ev.phases || [];
    phases.forEach(function (p, i) {
      var x0 = X(p.t0), x1 = X(i + 1 < phases.length ? phases[i + 1].t0 : p.t1);
      var w = Math.max(1, x1 - x0);
      if (p.preclose) svg.appendChild(s("rect", { x: x0, y: strip, width: w, height: H - strip - m.b, class: "preclose" }));
      svg.appendChild(s("rect", { x: x0, y: 0, width: w, height: strip - 4, class: p.preclose ? "phase-cell pre" : "phase-cell" }));
      var label = phaseName(p.name);
      var both = label + " · " + p.name;
      var text = w > label.length * 11.5 + p.name.length * 7 + 24 ? both : w > label.length * 11.5 + 10 ? label : w > 18 ? String(i + 1) : "";
      if (text) svg.appendChild(s("text", { x: x0 + w / 2, y: strip - 10, class: "phase-label", "text-anchor": "middle", text: text }));
    });
    // 축
    [[y1top, h1, fmax, Y1, 2], [y2top, h2, dmax, Y2, 1]].forEach(function (p) {
      [0, p[2] / 2, p[2]].forEach(function (v) {
        var y = p[3](v);
        svg.appendChild(s("line", { x1: px0, x2: px1, y1: y, y2: y, class: v === 0 ? "axis" : "grid" }));
        svg.appendChild(s("text", { x: px0 - 6, y: y + 4, class: "tick", "text-anchor": "end", text: fmt(v, v > 0 && v < 1 ? p[4] : 0) }));
      });
    });
    var step = niceMax(t1 / (compact ? 4 : 9));
    for (var tt = 0; tt <= tEnd + 1e-6; tt += step) {
      svg.appendChild(s("text", { x: X(tt), y: H - 6, class: "tick", "text-anchor": "middle", text: fmt(tt, step < 1 ? 1 : 0) + (tt + step > tEnd ? " s" : "") }));
    }
    svg.appendChild(s("text", { x: px0 + 4, y: y1top - 6, class: "panel-label", text: "패드 접촉력 (N)" }));
    svg.appendChild(s("text", { x: px0 + 4, y: y2top - 6, class: "panel-label", text: "컵 수평 이동 (mm, 시작 위치 기준)" }));
    // 임계선
    if (ev.threshold_n) {
      svg.appendChild(s("line", { x1: px0, x2: px1, y1: Y1(ev.threshold_n), y2: Y1(ev.threshold_n), class: "threshold" }));
      svg.appendChild(s("text", { x: px1 - 4, y: Y1(ev.threshold_n) - 4, class: "tick", "text-anchor": "end", text: "임계 " + fmt(ev.threshold_n, 2) + " N" }));
    }
    if (ev.preclose_disp_mm) {
      // 컵 이동 한계는 닫기 전 구간에만 적용하므로 그 구간에만 긋는다
      var lastPre = null;
      phases.forEach(function (p, i) {
        if (!p.preclose) return;
        var xa = X(p.t0), xb = X(i + 1 < phases.length ? phases[i + 1].t0 : p.t1);
        svg.appendChild(s("line", { x1: xa, x2: xb, y1: Y2(ev.preclose_disp_mm), y2: Y2(ev.preclose_disp_mm), class: "threshold" }));
        lastPre = xb;
      });
      if (lastPre !== null) {
        svg.appendChild(s("text", { x: lastPre - 4, y: Y2(ev.preclose_disp_mm) - 4, class: "tick", "text-anchor": "end", text: "닫기 전 이동 한계 " + fmt(ev.preclose_disp_mm, 0) + " mm" }));
      }
    }
    // 선
    function path(values, Y) {
      var d = "";
      T.forEach(function (t, i) { d += (i ? "L" : "M") + X(t).toFixed(1) + " " + Y(values[i] || 0).toFixed(1); });
      return d;
    }
    svg.appendChild(s("path", { d: path(fm, Y1), class: "line pad-moving" }));
    svg.appendChild(s("path", { d: path(f0, Y1), class: "line pad-fixed" }));
    svg.appendChild(s("path", { d: path(ev.cup_disp_mm || [], Y2), class: "line cup-disp" }));
    // 사건: 판정(붉은 실선) · 재계획(점선)
    var marks = [];
    (ev.events || []).forEach(function (e) {
      if (e.type === "retreat_started") return;
      var prev = marks[marks.length - 1];
      if (prev && Math.abs(prev.t - e.t_s) < 1e-3) return;
      marks.push({ t: e.t_s, type: e.type });
    });
    marks.forEach(function (mk, i) {
      var x = X(mk.t);
      var fail = mk.type === "preclose_failure";
      svg.appendChild(s("line", { x1: x, x2: x, y1: strip, y2: H - m.b, class: fail ? "verdict-line" : "event-line" }));
      var retreat = (ev.events || []).some(function (e) { return e.type === "retreat_started" && Math.abs(e.t_s - mk.t) < 1e-3; });
      var label = (fail ? (FAILURE[(ev.verdict || {}).failure_code] || "판정") + " 판정" + (retreat ? "·후퇴" : "") : EVENT[mk.type] || mk.type) + " " + fmt(mk.t, fail ? 3 : 2) + " s";
      var right = x > (px0 + px1) / 2;
      var ly = strip + 14 + (i % 2) * 14;
      svg.appendChild(s("text", { x: right ? x - 5 : x + 5, y: ly, class: fail ? "verdict-label" : "event-label", "text-anchor": right ? "end" : "start", text: label }));
    });
    var v = ev.verdict;
    if (v && v.pad) {
      var fv = (v.force_n || [])[PAD_INDEX[v.pad]];
      var vx = X(v.t_s), vy = Y1(fv);
      svg.appendChild(s("circle", { cx: vx, cy: vy, r: 4.5, class: "verdict-dot" }));
      var rightV = vx > (px0 + px1) / 2;
      svg.appendChild(s("text", { x: rightV ? vx - 8 : vx + 8, y: vy + 4, class: "verdict-value", "text-anchor": rightV ? "end" : "start", text: PAD[v.pad] + " " + f2(fv) + " N" }));
      svg.appendChild(s("text", { x: rightV ? vx - 8 : vx + 8, y: Y2(v.cup_disp_mm) - 6, class: "verdict-value", "text-anchor": rightV ? "end" : "start", text: "컵 " + f2(v.cup_disp_mm) + " mm" }));
    }
    var cross = s("line", { x1: -10, x2: -10, y1: strip, y2: H - m.b, class: "crosshair" });
    svg.appendChild(cross);
    var hit = s("rect", { x: px0, y: 0, width: px1 - px0, height: H, class: "hit" });
    svg.appendChild(hit);
    host.appendChild(svg);
    var tip = h("div", { class: "tooltip", role: "status", "aria-live": "off", hidden: true });
    host.appendChild(tip);
    hit.addEventListener("pointermove", function (evt) {
      var r = svg.getBoundingClientRect();
      var t = (evt.clientX - r.left) * (W / r.width);
      t = Math.min(tEnd, Math.max(0, (t - px0) / (px1 - px0) * t1));
      var best = 0;
      for (var i = 0; i < T.length; i++) if (Math.abs(T[i] - t) < Math.abs(T[best] - t)) best = i;
      var ph = phases.filter(function (p) { return p.t0 <= T[best] + 1e-6; }).pop();
      var x = X(T[best]);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x);
      tip.hidden = false;
      tip.textContent = "";
      append(tip, h("strong", { class: "num", text: fmt(T[best], 2) + " s" }));
      if (ph) append(tip, h("span", { class: "tip-row", text: phaseName(ph.name) + " (" + ph.name + ")" }));
      append(tip, h("span", { class: "tip-row" }, h("i", { class: "sw sw-fixed" }), PAD.fixed + " ", h("b", { class: "num", text: f2(f0[best]) + " N" })));
      append(tip, h("span", { class: "tip-row" }, h("i", { class: "sw sw-moving" }), PAD.moving + " ", h("b", { class: "num", text: f2(fm[best]) + " N" })));
      append(tip, h("span", { class: "tip-row" }, h("i", { class: "sw sw-disp" }), "컵 이동 ", h("b", { class: "num", text: fmt((ev.cup_disp_mm || [])[best], 3) + " mm" })));
      var left = x * (r.width / W);
      tip.classList.toggle("tip-left", left > r.width * 0.6);
      tip.style.left = left + "px";
    });
    hit.addEventListener("pointerleave", function () { tip.hidden = true; cross.setAttribute("x1", -10); cross.setAttribute("x2", -10); });
  };

  function phaseList(ev) {
    return h("ol", { class: "phase-list", "aria-label": "동작 단계" }, (ev.phases || []).map(function (p, i) {
      return h("li", { class: p.preclose ? "pre" : null },
        h("span", { class: "phase-no num", text: String(i + 1) }),
        phaseName(p.name) + (p.attempt ? "(재시도)" : ""),
        h("code", { class: "muted", text: " " + p.name }),
        h("span", { class: "muted num", text: " " + fmt(p.t0, 2) + "–" + fmt(p.t1, 2) + " s" }));
    }));
  }

  // 위에서 본 배치(실제 비율). x → 오른쪽, y → 위쪽(통과 지도와 같은 방향).
  function topView(geom, opts) {
    opts = opts || {};
    var snaps = geom.snapshots || [];
    if (!snaps.length) return null;
    var last = snaps[snaps.length - 1];
    var r = geom.cup_radius_mm, plan = geom.planned_cup_mm;
    var pts = [[plan[0] - r, plan[1] - r], [plan[0] + r, plan[1] + r]];
    snaps.forEach(function (sn) {
      pts.push([sn.cup_mm[0] - r, sn.cup_mm[1] - r], [sn.cup_mm[0] + r, sn.cup_mm[1] + r], [sn.tcp_mm[0], sn.tcp_mm[1]]);
      sn.pads.forEach(function (p) { p.top_mm.forEach(function (q) { pts.push(q); }); });
    });
    var minx = Math.min.apply(null, pts.map(function (p) { return p[0]; })) - 8;
    var maxx = Math.max.apply(null, pts.map(function (p) { return p[0]; })) + 8;
    var miny = Math.min.apply(null, pts.map(function (p) { return p[1]; })) - 16;
    var maxy = Math.max.apply(null, pts.map(function (p) { return p[1]; })) + 10;
    var k = 3.6;
    var W = Math.round((maxx - minx) * k), Hd = Math.round((maxy - miny) * k), H = Hd + 34;
    function X(x) { return (x - minx) * k; }
    function Y(y) { return (maxy - y) * k; }
    function poly(list) { return list.map(function (q) { return X(q[0]).toFixed(1) + "," + Y(q[1]).toFixed(1); }).join(" "); }
    var svg = s("svg", { viewBox: "0 0 " + W + " " + H, class: "schematic", role: "img",
      "aria-label": "위에서 본 컵과 패드 배치(실제 비율)" });
    svg.appendChild(s("circle", { cx: X(plan[0]), cy: Y(plan[1]), r: r * k, class: "cup-planned" }));
    svg.appendChild(s("text", { x: X(plan[0]), y: Y(plan[1] + r) - 6, class: "sch-label muted-label", "text-anchor": "middle", text: "계획 위치(점선)" }));
    var cup = last.cup_mm;
    svg.appendChild(s("circle", { cx: X(cup[0]), cy: Y(cup[1]), r: r * k, class: "cup-actual" }));
    svg.appendChild(s("text", { x: X(cup[0]), y: Y(cup[1]) + r * k * 0.45, class: "sch-label", "text-anchor": "middle", text: "실제 컵" }));
    var off = geom.offset_mm || [0, 0];
    if (Math.abs(off[0]) + Math.abs(off[1]) > 0) {
      svg.appendChild(s("line", { x1: X(plan[0]), y1: Y(plan[1]), x2: X(plan[0] + off[0]), y2: Y(plan[1] + off[1]), class: "offset-arrow" }));
      svg.appendChild(s("circle", { cx: X(plan[0]), cy: Y(plan[1]), r: 2.5, class: "center-dot planned" }));
      svg.appendChild(s("text", { x: X(cup[0]), y: Y(cup[1]) + r * k * 0.45 + 18, class: "sch-small", "text-anchor": "middle", text: "옮긴 거리 " + offsetText(off) }));
    }
    svg.appendChild(s("circle", { cx: X(cup[0]), cy: Y(cup[1]), r: 2.5, class: "center-dot" }));
    snaps.forEach(function (sn, si) {
      var faint = si < snaps.length - 1;
      sn.pads.forEach(function (p) {
        svg.appendChild(s("polygon", { points: poly(p.top_mm), class: "pad pad-" + p.key + (faint ? " faint" : "") }));
      });
      if (sn.target_mm && (si > 0 || snaps.length === 1)) {
        var tx = X(sn.target_mm[0]), ty = Y(sn.target_mm[1]);
        svg.appendChild(s("path", { d: "M" + (tx - 5) + " " + ty + "L" + tx + " " + (ty - 5) + "L" + (tx + 5) + " " + ty + "L" + tx + " " + (ty + 5) + "Z", class: "target-mark" }));
      }
    });
    // 패드 이름·여유(마지막 스냅샷)
    last.pads.forEach(function (p) {
      var xs = p.top_mm.map(function (q) { return q[0]; }), ys = p.top_mm.map(function (q) { return q[1]; });
      var cx = (Math.min.apply(null, xs) + Math.max.apply(null, xs)) / 2;
      var below = p.key === "fixed";
      var ly = below ? Y(Math.min.apply(null, ys)) + 16 : Y(Math.max.apply(null, ys)) - 8;
      var gapText = p.gap_mm < 0 ? "겹침 " + fmt(-p.gap_mm, 1) + " mm" : "여유 " + fmt(p.gap_mm, 1) + " mm";
      // 도식 가장자리에서 글자가 잘리지 않게 가운데 쪽으로 당긴다
      var lx = Math.min(W * 0.7, Math.max(W * 0.3, X(cx)));
      svg.appendChild(s("text", { x: lx, y: ly, class: "sch-label pad-label-" + p.key, "text-anchor": "middle", text: PAD[p.key] + " · " + gapText }));
    });
    var tcp = last.tcp_mm;
    svg.appendChild(s("path", { d: "M" + (X(tcp[0]) - 5) + " " + Y(tcp[1]) + "H" + (X(tcp[0]) + 5) + "M" + X(tcp[0]) + " " + (Y(tcp[1]) - 5) + "V" + (Y(tcp[1]) + 5), class: "tcp-mark" }));
    if (last.contact_mm) {
      var cxp = X(last.contact_mm[0]), cyp = Y(last.contact_mm[1]);
      svg.appendChild(s("circle", { cx: cxp, cy: cyp, r: 9, class: "contact-ring" }));
      svg.appendChild(s("text", { x: cxp + 13, y: cyp - 10, class: "sch-label contact-label", text: "접촉(추정)" }));
    }
    axesStrip(svg, geom.axes, W, H, k, 10, "x", "y");
    return svg;
  }

  // 도식 아래 띠: 축 방향과 축척(그림과 겹치지 않게 따로 둔다)
  function axesStrip(svg, axes, W, H, k, scaleMm, hAxis, vAxis) {
    var y = H - 12;
    svg.appendChild(s("line", { x1: 0, x2: W, y1: H - 34, y2: H - 34, class: "strip-rule" }));
    function word(axis) { return axes ? "로봇 " + axes[axis + "_plus_ko"] : "+" + axis; }
    svg.appendChild(s("text", { x: 10, y: y, class: "sch-small", text: "가로 " + hAxis + " → " + (hAxis === "z" ? "위" : word(hAxis)) + " · 세로 " + vAxis + " ↑ " + (vAxis === "z" ? "위" : word(vAxis)) }));
    svg.appendChild(s("line", { x1: W - 12 - scaleMm * k, x2: W - 12, y1: y - 4, y2: y - 4, class: "scale-bar" }));
    svg.appendChild(s("text", { x: W - 18 - scaleMm * k, y: y, class: "sch-small", "text-anchor": "end", text: scaleMm + " mm" }));
  }

  // 앞에서 본 확대: 접촉(또는 가장 가까운) 점을 지나는 x 평면. 가로 = y, 세로 = z.
  function frontView(geom, sn) {
    if (!sn || !sn.section) return null;
    var sec = sn.section;
    var fixedPad = sn.pads.filter(function (p) { return p.key === (sn.contact_pad || "fixed"); })[0] || sn.pads[0];
    var pz = fixedPad.front_mm.map(function (q) { return q[1]; });
    var pz0 = Math.min.apply(null, pz), pz1 = Math.max.apply(null, pz);
    var rim = sec.cup_z_mm[1];
    var z0, z1;
    if (Math.abs(pz0 - rim) < 5) { z0 = rim - 14; z1 = pz1 + 8; } else { z0 = pz0 - 12; z1 = pz1 + 12; }
    var W = 420, k = Math.min(W / 30, 300 / (z1 - z0));
    var H = Math.round((z1 - z0) * k) + 34;
    var yspan = W / k, y0 = sec.focus_y_mm - yspan / 2;
    function X(y) { return (y - y0) * k; }
    function Z(z) { return (z1 - z) * k; }
    var svg = s("svg", { viewBox: "0 0 " + W + " " + H, class: "schematic", role: "img", "aria-label": "앞에서 본 접촉 부위 확대(단면)" });
    var clip = "sec-" + Math.random().toString(36).slice(2, 8);
    svg.appendChild(s("defs", null, s("clipPath", { id: clip }, s("rect", { x: 0, y: 0, width: W, height: H - 34 }))));
    var g = s("g", { "clip-path": "url(#" + clip + ")" });
    g.appendChild(s("rect", { x: X(sec.cup_y_mm[0]), y: Z(sec.cup_z_mm[1]), width: (sec.cup_y_mm[1] - sec.cup_y_mm[0]) * k, height: (sec.cup_z_mm[1] - sec.cup_z_mm[0]) * k, class: "cup-actual" }));
    sn.pads.forEach(function (p) {
      g.appendChild(s("polygon", { points: p.front_mm.map(function (q) { return X(q[0]).toFixed(1) + "," + Z(q[1]).toFixed(1); }).join(" "), class: "pad pad-" + p.key }));
    });
    svg.appendChild(g);
    if (rim <= z1 && rim >= z0) {
      svg.appendChild(s("text", { x: W - 10, y: Z(rim) + 18, class: "sch-small", "text-anchor": "end", text: "컵 윗면(테두리)" }));
    }
    svg.appendChild(s("text", { x: Math.max(X(sec.cup_y_mm[0]) + 10, W * 0.55), y: Math.min(H - 44, Z(Math.max(z0, sec.cup_z_mm[0])) - 10), class: "sch-label", text: "컵" }));
    var fy = fixedPad.front_mm.map(function (q) { return q[0]; });
    svg.appendChild(s("text", { x: X((Math.min.apply(null, fy) + Math.max.apply(null, fy)) / 2), y: Z(pz1) - 6, class: "sch-label pad-label-" + fixedPad.key, "text-anchor": "middle", text: PAD[fixedPad.key] }));
    if (sn.contact_mm && Math.abs(pz0 - rim) < 5) {
      var cx = X(sn.section.focus_y_mm), cz = Z(rim);
      svg.appendChild(s("circle", { cx: cx, cy: cz, r: 9, class: "contact-ring" }));
      svg.appendChild(s("text", { x: cx - 14, y: cz + 26, class: "sch-label contact-label", "text-anchor": "end", text: "접촉(추정)" }));
    }
    axesStrip(svg, geom.axes, W, H, k, 5, "y", "z");
    return svg;
  }

  function frontCaption(sn) {
    var pad = sn.pads.filter(function (p) { return p.key === (sn.contact_pad || "fixed"); })[0] || sn.pads[0];
    var gap = pad.gap_mm < 0 ? "패드가 컵 테두리 안쪽으로 " + fmt(-pad.gap_mm, 1) + " mm 들어와 있음" : "패드와 컵 사이 " + fmt(pad.gap_mm, 1) + " mm";
    var rim = Math.abs(pad.bottom_above_rim_mm) < 5
      ? ", 패드 아래면이 컵 윗면보다 " + fmt(pad.bottom_above_rim_mm, 2) + " mm 위"
      : ", 패드 아래면이 컵 윗면보다 " + fmt(-pad.bottom_above_rim_mm, 0) + " mm 아래(컵 옆)";
    return "x = " + fmt(sn.section.x_mm, 1) + " mm 평면. " + gap + rim + ".";
  }

  function evidenceHeadline(ev) {
    var v = ev.verdict, out = ev.outcome || {};
    var peaks = ev.preclose_peaks || [];
    var lastPeak = peaks[peaks.length - 1] || {};
    var th = fmt(ev.threshold_n, 2) + " N";
    if (v && !out.task_pass) {
      return fmt(v.t_s, 3) + " s, " + phaseName(v.phase) + "(" + v.phase + ") 중 " + (PAD[v.pad] || "패드") + "가 " +
        f2(Math.max.apply(null, v.force_n)) + " N으로 컵에 닿았고, 그때 컵은 " + f2(v.cup_disp_mm) + " mm 움직였습니다. 닫기 전 구간에서 " +
        th + " 이상이면 " + (FAILURE[v.failure_code] || v.failure_code) + "으로 판정합니다.";
    }
    if (v && out.task_pass) {
      var re = (ev.events || []).filter(function (e) { return e.type === "replanned"; })[0];
      return "첫 시도 " + fmt(v.t_s, 3) + " s에 같은 접촉(" + (PAD[v.pad] || "패드") + " " + f2(Math.max.apply(null, v.force_n)) + " N)이 판정되자 후퇴했고, " +
        (re ? fmt(re.t_s, 2) + " s에 시뮬 정답 좌표로 다시 계획했습니다. " : "") +
        "다시 계획한 시도의 닫기 전 최대 힘은 " + f2(lastPeak.force_n) + " N(임계 " + th + ")이고, 닫은 뒤 들어 올려 통과했습니다.";
    }
    if (out.task_pass) {
      return "닫기 전 최대 힘 " + f2(lastPeak.force_n) + " N(임계 " + th + "), 닫기 전 컵 이동 최대 " + f2(lastPeak.cup_disp_mm) + " mm. 닫은 뒤 들어 올려 통과했습니다.";
    }
    return "판정: " + (out.stop_reason || "–");
  }

  function geometryCaption(geom) {
    var snaps = geom.snapshots || [];
    var last = snaps[snaps.length - 1];
    if (!last) return null;
    var fixed = last.pads.filter(function (p) { return p.key === (last.contact_pad || "fixed"); })[0] || last.pads[0];
    var when = last.kind === "verdict" ? "판정 순간(" + fmt(last.t_s, 3) + " s)" : "닫기 전 패드가 컵에 가장 가까웠던 순간(" + fmt(last.t_s, 2) + " s, " + phaseName(last.phase) + ")";
    var text = when + " " + PAD[fixed.key] + "는 컵 테두리와 " + (fixed.gap_mm < 0 ? fmt(-fixed.gap_mm, 1) + " mm 겹쳤고" : fmt(fixed.gap_mm, 1) + " mm 떨어져 있었고");
    if (last.kind === "verdict" && Math.abs(fixed.bottom_above_rim_mm) < 5) {
      text += ", 패드 아래면은 컵 윗면보다 " + fmt(fixed.bottom_above_rim_mm, 2) + " mm 위에 있었습니다. 내려오던 패드 아래 모서리가 컵 테두리 윗면에 닿은 것으로 봅니다(추정).";
    } else {
      text += ", 패드 아래면은 컵 윗면보다 " + fmt(-fixed.bottom_above_rim_mm, 0) + " mm 아래(컵 옆)에 있었습니다.";
    }
    if (snaps.length > 1) text += " 흐린 윤곽은 첫 시도(" + fmt(snaps[0].t_s, 3) + " s)의 패드, 마름모는 다시 계획한 접촉 중심 목표입니다.";
    return text;
  }

  // ev = 격자점 증거 문서. opts.title, opts.compact
  function evidenceBlock(ev, opts) {
    opts = opts || {};
    var wrap = h("div", { class: "evidence" });
    append(wrap, h("p", { class: "evidence-headline", text: evidenceHeadline(ev) }));
    if (ev.verdict && !(ev.outcome || {}).task_pass && ev.verdict.cup_disp_mm < 1) {
      append(wrap, h("p", { class: "note", text: "컵이 " + f2(ev.verdict.cup_disp_mm) + " mm만 움직여 영상에서는 거의 보이지 않습니다. 아래 힘 그래프와 배치도로 접촉을 확인합니다." }));
    }
    var legend = h("ul", { class: "legend", "aria-label": "범례" },
      h("li", null, h("i", { class: "sw sw-fixed" }), PAD.fixed + " 접촉력"),
      h("li", null, h("i", { class: "sw sw-moving" }), PAD.moving + " 접촉력"),
      h("li", null, h("i", { class: "sw sw-disp" }), "컵 수평 이동"),
      h("li", null, h("i", { class: "sw sw-pre" }), "닫기 전 구간(규칙 적용)"),
      h("li", null, h("i", { class: "sw sw-verdict" }), "판정 시각"),
      h("li", null, h("i", { class: "sw sw-threshold" }), "임계")
    );
    var host = h("div", { class: "graph contact-graph" });
    append(wrap, h("figure", { class: "evidence-plot" }, legend, host,
      h("details", { class: "phase-details", open: true }, h("summary", null, "단계 번호와 원래 이름"), phaseList(ev))));
    var geom = ev.geometry;
    if (geom && (geom.snapshots || []).length) {
      var snaps = geom.snapshots;
      var top = topView(geom), front = frontView(geom, snaps[snaps.length - 1]);
      append(wrap, h("div", { class: "schematics" },
        h("figure", { class: "sch-fig" }, h("figcaption", { class: "sch-title", text: "위에서 본 배치(실제 비율)" }), top,
          h("ul", { class: "legend sch-legend" },
            h("li", null, h("i", { class: "sw sw-planned" }), "계획 위치"),
            h("li", null, h("i", { class: "sw sw-cup" }), "실제 컵"),
            h("li", null, h("i", { class: "sw sw-pad-fixed" }), PAD.fixed),
            h("li", null, h("i", { class: "sw sw-pad-moving" }), PAD.moving),
            h("li", null, "＋ 접촉 중심(TCP)"),
            h("li", null, "◇ 접촉 중심 목표"),
            h("li", null, h("i", { class: "sw sw-contact" }), "접촉(추정)"))),
        front ? h("figure", { class: "sch-fig" }, h("figcaption", { class: "sch-title", text: "앞에서 본 확대(접촉 위치를 지나는 단면)" }), front,
          h("p", { class: "sch-note", text: frontCaption(snaps[snaps.length - 1]) })) : null
      ));
      append(wrap, h("p", { class: "evidence-geom", text: geometryCaption(geom) }));
      append(wrap, h("p", { class: "note", text: "패드 위치는 결과 샘플에 없어 계산했습니다: 샘플의 접촉 중심 위치·그리퍼 각 + 계획 자세의 접근축·닫힘축 + URDF의 가정 패드 상자. 축 방향은 URDF의 좌·우 팔 장착 위치(y 부호)로 정했습니다." }));
    } else {
      append(wrap, h("p", { class: "note", text: "이 격자점은 패드 기하를 계산할 자료(plan.json·URDF)가 없어 배치도를 생략했습니다." }));
    }
    var plot = new ContactPlot(host, ev);
    var onResize = debounce(function () { plot.render(); }, 150);
    window.addEventListener("resize", onResize);
    state.cleanup.push(function () { window.removeEventListener("resize", onResize); });
    // 붙인 뒤 너비가 정해지면 다시 그린다
    requestAnimationFrame(function () { plot.render(); });
    return wrap;
  }

  // ------------------------------------------------------------ 사례 ② 흐름(기록 → 재현 → 격자 A → 격자 B)
  function storyFlow(reg, card, current) {
    var st = reg && reg.story;
    if (!st) return null;
    var rec = st.recorded, a = (st.cells || {}).A || {}, b = (st.cells || {}).B || {};
    var ca = (reg.conditions || {}).A || {}, cb = (reg.conditions || {}).B || {};
    var p = st.point || {};
    var av = a.verdict || {};
    var labels = card.conditions || {};
    var readingA = (ca.readings || [])[0];
    var steps = [
      {
        n: 1, title: "기록된 사고", href: "#/s/" + card.scenario_id,
        lines: rec ? [
          h("code", { text: rec.run_name }),
          "컵 오프셋 " + offsetText(rec.offset_mm) + " · 복구 " + (rec.recovery_enabled ? "켬" : "끔"),
          h("strong", { text: fmt(rec.t_s, 3) + " s " + (FAILURE[rec.failure_code] || rec.failure_code) }),
          PAD[rec.pad] ? PAD[rec.pad] + " " + f2(Math.max.apply(null, rec.force_n)) + " N" : null
        ] : [st.incident ? fmt(st.incident.t_start_s, 3) + " s " + (FAILURE[st.incident.code] || st.incident.code) : "기록 파일 없음"]
      },
      {
        n: 2, title: "같은 조건 재현", href: "#/s/" + card.scenario_id,
        lines: [
          "조건 A " + shortLabel((labels.A || {}).label_ko) + " · 격자점 x " + mm(p.x_mm) + ", y " + mm(p.y_mm) + " mm",
          av.t_s !== undefined ? h("strong", { text: fmt(av.t_s, 3) + " s " + (FAILURE[av.failure_code] || av.failure_code) }) : (CELL[a.state] || "–"),
          av.pad ? PAD[av.pad] + " " + f2(Math.max.apply(null, av.force_n)) + " N" : null,
          st.same_time ? "기록과 같은 시각·같은 판정" : null
        ]
      },
      {
        n: 3, title: "주변 25점으로 확장", href: "#/eval/" + card.scenario_id,
        lines: [
          "컵 위치를 격자로 옮겨 칸마다 1회 실행",
          h("strong", { text: "조건 A " + shortLabel((labels.A || {}).label_ko) + " " + ca.pass + "/" + ca.valid }),
          readingA ? readingA.text_ko.split(" → ")[0] : ((ca.pattern_ko || [])[0] || null)
        ]
      },
      {
        n: 4, title: "복구 켬 비교", href: "#/eval/" + card.scenario_id,
        lines: [
          "조건 B " + shortLabel((labels.B || {}).label_ko) + "(시뮬 정답 좌표로 재계획)",
          h("strong", { text: "조건 B " + cb.pass + "/" + cb.valid }),
          b.state === "pass" && (b.events || []).some(function (e) { return e.type === "replanned"; })
            ? "같은 격자점: 접촉 판정 → 후퇴 → 재계획 → 들어 올림" : null
        ]
      }
    ];
    return h("nav", { class: "flow", "aria-label": "사례 ② 흐름" }, h("ol", null, steps.map(function (st2) {
      var on = current && current.indexOf(st2.n) >= 0;
      return h("li", { class: on ? "flow-step on" : "flow-step" },
        h("a", { href: st2.href, "aria-current": on ? "step" : null },
          h("span", { class: "flow-no num", text: String(st2.n) }),
          h("span", { class: "flow-title", text: st2.title }),
          h("span", { class: "flow-body" }, st2.lines.filter(Boolean).map(function (l) { return h("span", { class: "flow-line" }, l); }))
        ));
    })));
  }

  // ------------------------------------------------------------ 격자 읽는 법(실제 비율 도식)
  function readingDiagram(reg, ev) {
    var grid = reg.grid || {};
    var geom = ev && ev.geometry;
    if (!geom || !(geom.snapshots || []).length) return null;
    var sn = geom.snapshots[0];
    var r = geom.cup_radius_mm, plan = geom.planned_cup_mm, cup = sn.cup_mm;
    var fixed = sn.pads.filter(function (p) { return p.key === "fixed"; })[0];
    var pts = [[plan[0] - r, plan[1] - r], [plan[0] + r, plan[1] + r], [cup[0] - r, cup[1] - r]];
    if (fixed) fixed.top_mm.forEach(function (q) { pts.push(q); });
    var minx = Math.min.apply(null, pts.map(function (p) { return p[0]; })) - 6;
    var maxx = Math.max.apply(null, pts.map(function (p) { return p[0]; })) + 6;
    var miny = Math.min.apply(null, pts.map(function (p) { return p[1]; })) - 14;
    var maxy = Math.max.apply(null, pts.map(function (p) { return p[1]; })) + 14;
    var k = 4;
    var W = Math.round((maxx - minx) * k), H = Math.round((maxy - miny) * k) + 34;
    function X(x) { return (x - minx) * k; }
    function Y(y) { return (maxy - y) * k; }
    var svg = s("svg", { viewBox: "0 0 " + W + " " + H, class: "schematic reading-svg", role: "img",
      "aria-label": "격자 25점을 실제 비율로 그린 도식: 계획 위치의 컵, 옮긴 컵 중심 25곳, 판정 순간의 고정 죠 패드" });
    svg.appendChild(s("circle", { cx: X(plan[0]), cy: Y(plan[1]), r: r * k, class: "cup-planned" }));
    svg.appendChild(s("circle", { cx: X(cup[0]), cy: Y(cup[1]), r: r * k, class: "cup-actual" }));
    if (fixed) svg.appendChild(s("polygon", { points: fixed.top_mm.map(function (q) { return X(q[0]).toFixed(1) + "," + Y(q[1]).toFixed(1); }).join(" "), class: "pad pad-fixed" }));
    var cells = {};
    ((reg.conditions || {}).A || {}).cells.forEach(function (c) { cells[Number(c.x_mm) + "," + Number(c.y_mm)] = c; });
    (grid.x_mm || []).forEach(function (x) {
      (grid.y_mm || []).forEach(function (y) {
        var c = cells[Number(x) + "," + Number(y)];
        var st = c && c.verdict === "fail" ? "fail" : "pass";
        svg.appendChild(s("circle", { cx: X(plan[0] + x), cy: Y(plan[1] + y), r: 2.6, class: "grid-dot dot-" + st }));
      });
    });
    svg.appendChild(s("text", { x: X(plan[0]), y: Y(plan[1] + r) - 8, class: "sch-label muted-label", "text-anchor": "middle", text: "계획 위치(점선) — 로봇은 이 원을 기준으로 움직임" }));
    svg.appendChild(s("text", { x: X(plan[0]) + 18, y: Y(plan[1]) - 16, class: "sch-small", text: "컵 중심을 옮긴 25곳(점)" }));
    if (fixed) {
      var fx = fixed.top_mm.map(function (q) { return q[0]; }), fy = fixed.top_mm.map(function (q) { return q[1]; });
      svg.appendChild(s("text", { x: X((Math.min.apply(null, fx) + Math.max.apply(null, fx)) / 2), y: Y(Math.min.apply(null, fy)) + 17, class: "sch-label pad-label-fixed", "text-anchor": "middle", text: PAD.fixed + "(판정 순간)" }));
    }
    svg.appendChild(s("text", { x: X(cup[0]), y: Y(cup[1] - r) - 10, class: "sch-small", "text-anchor": "middle", text: "옮긴 컵(" + offsetText(geom.offset_mm) + ")" }));
    axesStrip(svg, geom.axes, W, H, k, 10, "x", "y");
    return svg;
  }

  function readingBlock(reg, card) {
    var grid = reg.grid || {};
    var step = null;
    var xs = (grid.x_mm || []).slice().sort(function (a, b) { return a - b; });
    for (var i = 1; i < xs.length; i++) { var d = xs[i] - xs[i - 1]; if (d > 0 && (step === null || d < step)) step = d; }
    var axes = reg.axes;
    var conds = card.conditions || {};
    var diagramHost = h("div", { class: "reading-figure" }, h("p", { class: "note", text: "도식을 불러오는 중입니다." }));
    var gaps = ((reg.line_gaps || {}).y) || [];
    var check = reg.geometry_check;
    var readings = (((reg.conditions || {}).A || {}).readings) || [];
    var block = h("section", { class: "reading", "aria-labelledby": "reading-title" },
      h("h2", { id: "reading-title", text: "격자 읽는 법" }),
      h("div", { class: "reading-grid" },
        diagramHost,
        h("div", { class: "reading-text" },
          h("ul", { class: "reading-list" },
            h("li", null, h("strong", { text: "칸 하나 = 컵을 그 위치로 옮겨 놓고 Isaac을 한 번 실행한 결과" }),
              "입니다. 칸의 x·y는 계획 위치에서 컵을 옮긴 거리(mm)이고, " + (xs.length ? xs.length + "×" + (grid.y_mm || []).length + " = " + xs.length * (grid.y_mm || []).length + "칸, " : "") + (step ? "간격 " + fmt(step, 1) + " mm" : "") + "입니다."),
            h("li", null, h("strong", { text: "로봇은 원래 위치(점선)를 기준으로 움직입니다." }),
              " 조건 A(" + shortLabel((conds.A || {}).label_ko) + ")는 옮긴 사실을 모른 채 계획대로 내려가고, 조건 B(" + shortLabel((conds.B || {}).label_ko) + ")는 닫기 전 접촉이 판정되면 물러나 시뮬 정답 좌표로 다시 계획합니다."),
            h("li", null, "축: " + (axes ? "x + = 로봇 " + axes.x_plus_ko + ", y + = 로봇 " + axes.y_plus_ko + "(URDF의 좌·우 팔 장착 위치로 확인)" : "월드 x·y(로봇 기준 방향은 확인하지 못했습니다)") + ". 아래 지도도 같은 방향(x 가로, y 세로·위가 +)입니다.")
          ),
          readings.length ? h("p", { class: "reading-line" }, readings.map(function (r) { return h("span", { text: r.text_ko }); })) : null,
          gaps.length ? h("div", { class: "gap-table-wrap" },
            h("p", { class: "note", text: "설정값으로 계산한 닫기 전 최소 여유(조건 A, 가운데 열 x " + mm(gaps[0].x_mm) + " mm, " + PAD[gaps[0].pad] + " 쪽)" }),
            h("table", { class: "table gap-table" },
              h("thead", null, h("tr", null, h("th", { scope: "col", text: "y 오프셋" }), h("th", { scope: "col", text: "계산 여유" }), h("th", { scope: "col", text: "결과" }))),
              h("tbody", null, gaps.slice().reverse().map(function (g) {
                return h("tr", { class: g.gap_mm < 0 ? "row-cause" : null },
                  h("td", { class: "num", text: mm(g.y_mm) + " mm" }),
                  h("td", { class: "num", text: (g.gap_mm < 0 ? "겹침 " + fmt(-g.gap_mm, 1) : fmt(g.gap_mm, 1)) + " mm" }),
                  h("td", null, h("span", { class: "state state-" + g.state, text: CELL[g.state] || g.state })));
              }))
            ),
            check ? h("p", { class: "note", text: "계산 여유가 0 미만인 칸과 조건 A에서 실패한 칸이 " + check.total + "칸 중 " + check.agree + "칸 일치합니다. 격자로 본 범위와 같은 결론이고, 계산값은 설정·계획 축으로 구한 추정입니다." }) : null
          ) : null
        )
      )
    );
    var storyA = ((reg.story || {}).cells || {}).A;
    var evPath = storyA && storyA.evidence;
    if (evPath) {
      getJSON(evPath).then(function (ev) {
        diagramHost.textContent = "";
        var svg = readingDiagram(reg, ev);
        if (svg) {
          append(diagramHost, svg);
          append(diagramHost, h("p", { class: "note", text: "실제 비율. 점 25곳은 컵 중심을 옮긴 위치(붉은 점 = 조건 A 실패)이고, 채운 원은 기록된 사고와 같은 위치(" + offsetText(ev.geometry.offset_mm) + ")로 옮긴 컵입니다." }));
        }
      }).catch(function () { diagramHost.textContent = ""; });
    } else {
      diagramHost.textContent = "";
    }
    return block;
  }

  // ------------------------------------------------------------ 평가
  function gridCards() { return state.cards.filter(function (c) { return GRID_TASKS[c.task]; }); }

  function renderEval(main, sid, sel) {
    var cards = gridCards();
    var card = cards.filter(function (c) { return c.scenario_id === sid; })[0] || cards.filter(function (c) { return c.task === "cup_contact"; })[0] || cards[0];
    main.appendChild(h("header", { class: "detail-head" },
      h("h1", { tabindex: "-1", text: "격자 평가" }),
      h("p", { class: "lead", text: "같은 사고를 위치 오프셋 5×5 격자에서 두 조건으로 다시 실행하고, 시뮬레이터 실좌표 결과 파일로 통과·실패를 판정했습니다." })
    ));
    if (!card) {
      main.appendChild(h("p", { class: "empty", text: "이 빌드에는 격자 평가 대상 시나리오가 없습니다." }));
      return Promise.resolve();
    }
    if (cards.length > 1) {
      main.appendChild(h("div", { class: "segmented", role: "tablist", "aria-label": "시나리오 선택" }, cards.map(function (c) {
        var on = c === card;
        return h("a", { role: "tab", "aria-selected": on ? "true" : "false", class: on ? "seg on" : "seg", href: "#/eval/" + c.scenario_id, text: c.title_ko });
      })));
    }
    var load = card.has_regress ? getJSON("data/regress/" + encodeURIComponent(card.scenario_id) + ".json") : Promise.resolve(null);
    return load.then(function (reg) {
      var flow = storyFlow(reg, card, [3, 4]);
      if (flow) main.appendChild(flow);
      main.appendChild(h("div", { class: "eval-title" }, h("h2", { text: card.title_ko }), conditionBadges(card)));
      renderMaps(main, card, reg, sel);
    });
  }

  function renderMaps(main, card, reg, sel) {
    var conds = card.conditions || {};
    var names = Object.keys(conds);
    var grid = reg ? reg.grid : null;
    var panel = h("section", { class: "cell-panel", "aria-live": "polite" },
      h("p", { class: "note", text: reg ? "칸을 누르면 그 격자점의 검사값·임계값·실행 기록이 여기에 나옵니다." : "격자 실행 결과가 들어오면 칸마다 검사값과 실행 기록을 볼 수 있습니다." }));
    if (reg) {
      var parts = names.map(function (n) {
        var c = (reg.conditions || {})[n] || {};
        return h("span", { class: "sum-part" }, "조건 " + n + " " + (conds[n].label_ko || "") + " ",
          h("strong", { class: "num", text: (c.pass !== undefined ? c.pass + "/" + c.valid : "–") }),
          c.invalid ? h("span", { class: "warn", text: " 지도 무효" }) : null);
      });
      var det = reg.determinism || {};
      var agree = det.verdict_agreement !== undefined && det.verdict_agreement !== null
        ? h("span", { class: "sum-part" }, "판정 일치율 ", h("strong", { class: "num", text: fmt(det.verdict_agreement * 100, 0) + "%" }),
          h("span", { class: "muted", text: " (조건 " + (det.condition || "A") + ", " + det.points + "점 × " + det.repeats + "회)" }))
        : null;
      var need = (grid ? grid.x_mm.length * grid.y_mm.length : 25);
      var partial = reg.complete === false
        ? h("span", { class: "sum-part" }, h("span", { class: "pending", text: "부분 실행" }),
          " 조건별 " + need + "점 중 " + names.map(function (n) { return "조건 " + n + " " + ((((reg.conditions || {})[n] || {}).cells || []).length) + "점"; }).join(", ") + "만 실행됐습니다.")
        : null;
      var patterns = names.map(function (n) {
        var pk = ((reg.conditions || {})[n] || {}).pattern_ko || [];
        return pk.length ? "조건 " + n + " " + shortLabel(conds[n].label_ko) + ": " + pk.join(", ") : null;
      }).filter(Boolean);
      main.appendChild(h("p", { class: "eval-summary" }, parts.length === 2 ? [parts[0], h("span", { class: "arrow", "aria-hidden": "true", text: "→" }), parts[1]] : parts, agree, partial));
      if (patterns.length) main.appendChild(h("p", { class: "eval-pattern" }, patterns.map(function (t) { return h("span", { text: t }); })));
    } else {
      main.appendChild(h("p", { class: "eval-summary" }, h("span", { class: "pending", text: "측정 전" }), " 이 시나리오의 격자 실행 결과가 아직 없습니다. 아래 지도는 칸 배치만 보여 줍니다."));
    }
    if (reg && card.task === "cup_contact" && reg.complete) main.appendChild(readingBlock(reg, card));
    var axes = reg && reg.axes;
    var maps = h("div", { class: "maps" });
    var toSelect = null;
    var xs = grid ? grid.x_mm : [-5, -2.5, 0, 2.5, 5];
    var ys = (grid ? grid.y_mm : [-5, -2.5, 0, 2.5, 5]).slice().sort(function (a, b) { return b - a; });
    names.forEach(function (n) {
      var cond = reg ? (reg.conditions || {})[n] : null;
      var cells = {};
      ((cond && cond.cells) || []).forEach(function (c) { cells[Number(c.x_mm) + "," + Number(c.y_mm)] = c; });
      var gridEl = h("div", { class: "map-grid", role: "group", "aria-label": "조건 " + n + " 통과 지도" });
      gridEl.style.setProperty("--cols", xs.length);
      var badRows = ((cond && cond.failed_rows_mm) || []).map(Number);
      var badCols = ((cond && cond.failed_cols_mm) || []).map(Number);
      ys.forEach(function (y) {
        var rowBad = badRows.indexOf(Number(y)) >= 0;
        gridEl.appendChild(h("span", { class: rowBad ? "axis-y num line-fail" : "axis-y num", text: mm(y), title: rowBad ? "이 행 전부 실패" : null }));
        xs.forEach(function (x) {
          var c = cells[Number(x) + "," + Number(y)];
          var st = !c ? "none" : (c.status && c.status !== "ok") ? "infra" : (c.verdict === "pass" || c.verdict === "fail") ? c.verdict : "infra";
          var word = st === "none" ? "–" : CELL[st];
          var lineBad = badRows.indexOf(Number(y)) >= 0 || badCols.indexOf(Number(x)) >= 0;
          var gapTitle = c && c.min_gap_mm !== undefined && c.min_gap_mm !== null ? "계산 여유 " + fmt(c.min_gap_mm, 1) + " mm" : null;
          var btn = h("button", { type: "button", class: "cell cell-" + st + (lineBad ? " in-line-fail" : ""), disabled: !c, title: gapTitle, "aria-label": "x " + mm(x) + " mm, y " + mm(y) + " mm, " + (st === "none" ? "측정 전" : word) },
            h("span", { class: "cell-word", text: word }), c && c.video ? h("span", { class: "cell-rec", "aria-hidden": "true", title: "녹화 있음" }) : null);
          if (c) btn.addEventListener("click", function () {
            maps.querySelectorAll(".cell.sel").forEach(function (b) { b.classList.remove("sel"); });
            btn.classList.add("sel");
            showCell(panel, reg, n, conds[n], c);
            if (history.replaceState) history.replaceState(null, "", "#/eval/" + card.scenario_id + "/" + n + "/" + x + "/" + y);
          });
          if (c && sel && sel.cond === n && Number(sel.x) === Number(x) && Number(sel.y) === Number(y)) toSelect = btn;
          gridEl.appendChild(btn);
        });
      });
      gridEl.appendChild(h("span", { class: "axis-corner", "aria-hidden": "true" }));
      xs.forEach(function (x) { gridEl.appendChild(h("span", { class: badCols.indexOf(Number(x)) >= 0 ? "axis-x num line-fail" : "axis-x num", text: mm(x) })); });
      maps.appendChild(h("figure", { class: "map" },
        h("figcaption", null,
          h("span", { class: "map-name", text: "조건 " + n + "  " + (conds[n].label_ko || "") }),
          badge("basis", "판정 조건", BASIS[conds[n].judgement_basis] || conds[n].judgement_basis),
          cond ? h("span", { class: "map-count num", text: "통과 " + cond.pass + "/" + cond.valid + (cond.infra || cond.timeout ? "  infra " + ((cond.infra || 0) + (cond.timeout || 0)) : "") }) : null
        ),
        h("div", { class: "map-frame" },
          h("span", { class: "axis-title-y", text: "y 오프셋 (mm" + (axes ? ", + = 로봇 " + axes.y_plus_ko : "") + ")" }),
          gridEl,
          h("span", { class: "axis-title-x", text: "x 오프셋 (mm" + (axes ? ", + = 로봇 " + axes.x_plus_ko : "") + ")" })
        )
      ));
    });
    main.appendChild(maps);
    main.appendChild(h("ul", { class: "legend map-legend", "aria-label": "칸 범례" },
      h("li", null, h("i", { class: "sw cell-pass" }), "통과"),
      h("li", null, h("i", { class: "sw cell-fail" }), "실패"),
      h("li", null, h("i", { class: "sw cell-infra" }), "infra(도구 오류·시간 초과, k/valid에서 제외)"),
      h("li", null, h("i", { class: "sw sw-rec" }), "녹화 있음")
    ));
    main.appendChild(panel);
    if (toSelect) toSelect.click();
    if (reg && reg.pass_map_png) {
      main.appendChild(h("p", { class: "note" }, h("a", { class: "text-link", href: reg.pass_map_png, text: "통과 지도 이미지(PNG 1200×600)" })));
    }
    main.appendChild(h("details", { class: "sources" },
      h("summary", null, "판정 기준"),
      h("div", { class: "prov" },
        h("p", { text: "판정은 실행이 남긴 결과 파일만 읽습니다(시뮬레이터 실좌표). 조건별 판정 조건:" }),
        h("ul", null, names.map(function (n) { return h("li", null, "조건 " + n + " " + (conds[n].label_ko || "") + ": " + (BASIS[conds[n].judgement_basis] || conds[n].judgement_basis)); })),
        h("p", { text: "검사 항목: " + ((reg && reg.verifiers) || []).join(", ") }),
        h("p", { text: "도구 오류·시간 초과 칸은 infra로 칠하고 k/valid에서 뺍니다. 25점 중 infra가 3점을 넘으면 그 지도는 무효로 표시합니다. 판정 일치율은 같은 격자점을 반복 실행했을 때 판정이 같은 비율입니다." }),
        reg && reg.provenance ? h("p", { class: "note" }, "원천 커밋 ", h("code", { text: String(reg.provenance.source_repo_commit || "–").slice(0, 12) }), "  어댑터 SHA-256 ", h("code", { class: "sha", text: reg.provenance.adapter_sha256 || "–" })) : null
      )
    ));
  }

  function checkValue(v, unit) {
    if (v === null || v === undefined) return "–";
    if (typeof v === "boolean") return v ? "예" : "아니오";
    if (typeof v !== "number") return String(v);
    var text = Number(v).toLocaleString("ko-KR", { maximumFractionDigits: 3 });
    return unit && unit !== "bool" ? text + " " + unit : text;
  }

  function showCell(panel, reg, name, cond, cell) {
    panel.textContent = "";
    var st = cell.status && cell.status !== "ok" ? "infra(" + cell.status + ")" : CELL[cell.verdict] || cell.verdict;
    append(panel, h("h3", { text: "조건 " + name + "  x " + mm(cell.x_mm) + " mm, y " + mm(cell.y_mm) + " mm" }));
    append(panel, h("p", null, h("span", { class: "state state-" + (cell.status && cell.status !== "ok" ? "infra" : cell.verdict), text: st }),
      cell.failure_code ? h("code", { class: "code-inline", text: cell.failure_code }) : null));
    if (cell.video) {
      var cv = h("video", { src: cell.video, poster: cell.poster || null, controls: true, playsinline: true, muted: true, preload: "auto", class: "cell-video", "aria-label": "격자점 녹화(컵·그리퍼 주변 확대)" });
      append(panel, h("figure", { class: "cell-figure" }, cv,
        h("figcaption", { text: "녹화" + (cell.record_run_id ? "(녹화 전용 실행 " + cell.record_run_id + ")" : "") + ". 컵·그리퍼 주변을 잘라 확대했습니다." })));
      cv.muted = true;
      cv.play().catch(function () {});
    } else if (reg.complete) {
      append(panel, h("p", { class: "note", text: "이 격자점은 녹화하지 않았습니다. 녹화는 조건 A 실패점, 같은 점의 조건 B, 대표 통과점만 남깁니다." }));
    }
    if (cell.evidence) {
      var evHost = h("div", { class: "cell-evidence" }, h("p", { class: "note", text: "접촉 증거를 불러오는 중입니다." }));
      append(panel, evHost);
      getJSON(cell.evidence).then(function (ev) {
        evHost.textContent = "";
        append(evHost, h("h4", { class: "cell-sub", text: "접촉 증거" }));
        append(evHost, evidenceBlock(ev));
      }).catch(function (err) { evHost.textContent = "접촉 증거를 읽지 못했습니다: " + err.message; });
    }
    var cause = (cell.checks || []).filter(function (c) { return c.id === "failure_event"; })[0];
    var nUnmeasured = (cell.checks || []).filter(function (c) { return c.pass === null; }).length;
    if (cause && nUnmeasured) {
      append(panel, h("p", { class: "unmeasured-note", text: withRo(FAILURE[cause.value] || cause.value) + " 시뮬이 " +
        (cause.t_s !== null && cause.t_s !== undefined ? fmt(cause.t_s, 3) + " s에 " : "") +
        "끝나 이후 검사 " + nUnmeasured + "개는 측정되지 않았습니다." }));
    }
    append(panel, h("h4", { class: "cell-sub", text: "검사값" }));
    append(panel, h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, ["검사", "값", "임계", "결과"].map(function (t) { return h("th", { scope: "col", text: t }); }))),
      h("tbody", null, (cell.checks || []).map(function (c) {
        var unmeasured = c.pass === null || c.pass === undefined;
        var value = c.id === "failure_event"
          ? (FAILURE[c.value] || c.value) + (c.t_s !== null && c.t_s !== undefined ? ", " + fmt(c.t_s, 3) + " s" : "")
          : unmeasured ? "–" : checkValue(c.value, c.unit);
        return h("tr", { class: unmeasured ? "row-unmeasured" : c.id === "failure_event" ? "row-cause" : null },
          h("td", null, h("code", { class: "check-id", text: c.id })), h("td", { class: "num", text: value }),
          h("td", { class: "num", text: checkValue(c.threshold, c.unit) }),
          h("td", { text: unmeasured ? "측정 안 됨" : c.pass ? "통과" : "실패" }));
      }))
    )));
    append(panel, h("p", { class: "note" }, "실행 기록 ", h("code", { text: (reg.runs_root || "") + "/" + (cell.run_id || "") })));
    var target = panel.querySelector("h3");
    if (target && panel.getBoundingClientRect().top > window.innerHeight) panel.scrollIntoView({ block: "start" });
    if (target) target.setAttribute("tabindex", "-1");
  }

  // ------------------------------------------------------------ 라우터
  function debounce(fn, ms) { var id; return function () { clearTimeout(id); id = setTimeout(fn, ms); }; }

  function route() {
    state.cleanup.splice(0).forEach(function (fn) { try { fn(); } catch (e) { /* 이전 화면 정리 */ } });
    var main = document.getElementById("main");
    main.textContent = "";
    var hash = location.hash.replace(/^#\/?/, "");
    var parts = hash.split("/").filter(Boolean).map(decodeURIComponent);
    var nav = parts[0] === "eval" ? "eval" : "home";
    document.querySelectorAll("[data-nav]").forEach(function (a) {
      if (a.getAttribute("data-nav") === nav && parts[0] !== "s") a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    var job;
    if (parts[0] === "s" && parts[1]) {
      var known = state.cards.some(function (c) { return c.scenario_id === parts[1]; });
      job = known ? renderDetail(main, parts[1], parts[2]) : Promise.resolve(notFound(main));
      document.title = "사고 상세 | 사고 재생 리포트";
    } else if (parts[0] === "eval") {
      job = renderEval(main, parts[1], parts.length >= 5 ? { cond: parts[2], x: parts[3], y: parts[4] } : null);
      document.title = "격자 평가 | 사고 재생 리포트";
    } else {
      renderHome(main);
      job = Promise.resolve();
      document.title = "사고 재생 리포트";
    }
    var deepCell = parts[0] === "eval" && parts.length >= 5;
    job.then(function () {
      if (!deepCell) window.scrollTo(0, 0);
      var h1 = main.querySelector("h1");
      if (h1 && document.activeElement !== document.body) h1.focus({ preventScroll: true });
    }).catch(function (err) {
      main.textContent = "";
      main.appendChild(h("p", { class: "empty", text: "데이터를 읽지 못했습니다: " + err.message }));
    });
  }

  function notFound(main) {
    main.appendChild(h("h1", { tabindex: "-1", text: "없는 시나리오입니다" }));
    main.appendChild(h("p", null, h("a", { href: "#/", text: "사고 목록으로" })));
  }

  function boot() {
    Promise.all([
      getJSON("data/build.json"), getJSON("data/scenarios.json"), getJSON("data/hero.json"), getJSON("data/out_of_scope.json")
    ]).then(function (r) {
      state.build = r[0]; state.cards = r[1]; state.hero = r[2]; state.outOfScope = r[3];
      var foot = document.getElementById("colophon");
      foot.textContent = "";
      append(foot, h("p", null,
        (state.build.build === "private" ? "비공개 빌드(배포 금지)" : "공개 빌드") + ", " + (state.build.title_ko || state.build.profile) + ", 빌드 시각 " + String(state.build.built_at || "").replace("T", " ").slice(0, 16)
      ));
      if (state.build.build === "private") document.body.classList.add("is-private");
      window.addEventListener("hashchange", route);
      route();
    }).catch(function (err) {
      var main = document.getElementById("main");
      main.textContent = "";
      main.appendChild(h("p", { class: "empty", text: "리포트 데이터를 읽지 못했습니다(" + err.message + "). 로컬 서버로 열어 주세요." }));
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
