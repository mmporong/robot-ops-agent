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
  var FAILURE = { premature_cup_contact: "조기 접촉", not_arrived_within_horizon: "시간 내 미도착", reversal_rate_exceeded: "반전율 초과" };
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
      lines.push(h("p", { class: "hero-facts" },
        "SO-101 텔레옵 기록에서 관측 상태가 ",
        h("strong", { class: "num", text: fmt(hero.incident.t_start_s, 1) + "–" + fmt(hero.incident.t_end_s, 1) + " s" }),
        " 동안 멈춘 사이 명령은 계속 움직였습니다. 명령과 관측의 최대 TCP 거리 ",
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
      var head = "컵 위치 ±" + fmt(grid.range_mm, 0) + " mm 격자 " + grid.points + "점";
      var p = h("p", { class: hero.source === "cup_grid" ? "hero-facts" : "hero-facts hero-facts-minor" });
      append(p, head + " · ");
      if (cup.measured && a.valid !== undefined && b.valid !== undefined) {
        append(p, [h("span", { class: "nowrap" }, shortLabel(a.label_ko) + " ", h("strong", { class: "num", text: a.pass + "/" + a.valid })), " → ",
          h("span", { class: "nowrap" }, shortLabel(b.label_ko) + " ", h("strong", { class: "num", text: b.pass + "/" + b.valid }))]);
        var pat = (hero.pattern_ko || {}).A || [];
        if (hero.source === "cup_grid" && pat.length && pat[0] !== "실패 없음") {
          lines.push(p);
          p = h("p", { class: "hero-pattern", text: "조건 A " + shortLabel(a.label_ko) + ": " + pat.join(", ") + ". 조건 B에서는 같은 격자점이 " + (((hero.pattern_ko || {}).B || [])[0] === "실패 없음" ? "모두 통과했습니다." : "일부 실패했습니다.") });
        }
      } else {
        append(p, [h("span", { class: "pending", text: "측정 전" }), " 격자 실행 결과가 들어오면 조건별 통과 수를 표시합니다."]);
      }
      lines.push(p);
      lines.push(h("div", { class: "badges" },
        badge("origin", "출처", ORIGIN[cup.origin] || cup.origin),
        badge("basis", "조건 A", BASIS[a.judgement_basis] || a.judgement_basis),
        badge("basis", "조건 B", BASIS[b.judgement_basis] || b.judgement_basis),
        badge("assembly", "구성", ASSEMBLY[cup.assembly] || cup.assembly),
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
      hero.poster ? h("img", { src: hero.poster, alt: "", width: hero.width || 1280, height: hero.height || 480, decoding: "async" }) : null,
      h("span", { class: "play-mark", "aria-hidden": "true" })
    );
    btn.addEventListener("click", function () {
      var v = h("video", { controls: true, playsinline: true, preload: "auto", poster: hero.poster || null, src: hero.video, class: "hero-video" });
      fig.replaceChild(v, btn);
      v.play().catch(function () {});
      v.focus();
    });
    append(fig, btn);
    append(fig, h("figcaption", null, hero.source === "cup_grid"
      ? "조건 A 격자점 x " + mm((hero.point || {}).x_mm) + " mm, y " + mm((hero.point || {}).y_mm) + " mm 녹화, 같은 격자점 조건 B 녹화, 통과 지도 순서입니다. 녹화는 컵·그리퍼 주변을 잘라 확대했고 붉은 테두리가 판정 시각입니다."
      : "왼쪽 " + (hero.real_label_ko || "실측 카메라") + ", 오른쪽 고스트 렌더(명령 반투명, 관측 불투명). 붉은 띠가 관측 동결 구간입니다."));
    return fig;
  }

  // ------------------------------------------------------------ 홈(히어로 + 사고 목록)
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

    var list = h("ol", { class: "incident-list" });
    state.cards.forEach(function (card) {
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
    main.appendChild(h("section", { class: "incidents", "aria-labelledby": "list-title" },
      h("div", { class: "section-head" },
        h("h2", { id: "list-title", text: "사고 목록" }),
        h("p", { class: "count num", text: state.cards.length + "건" })
      ),
      state.cards.length ? list : h("p", { class: "empty", text: "이 빌드에 포함된 시나리오가 없습니다." }),
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
      else renderGridDetail(main, doc);
      renderSources(main, doc, view);
    });
  }

  function renderReplayView(main, doc, view) {
    var realCap = view.real.upscaled ? "원본 352×288을 높이 480으로 확대했습니다." : null;
    var real = videoFigure(view.real.src, view.real.poster, view.real.label_ko, realCap, "real");
    var ghost = view.ghost.src
      ? videoFigure(view.ghost.src, view.ghost.poster, view.ghost.label_ko, MIRROR_CAPTION, "ghost")
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
      h("p", { class: "note", text: "그래프를 누르면 두 영상이 그 시각으로 이동합니다. 교정 차(정적 구간 평균 명령−관측)를 뺀 뒤의 오차입니다." })
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
    main.appendChild(h("details", { class: "sources" }, h("summary", null, "출처"), body));
  }

  // ------------------------------------------------------------ 평가
  function gridCards() { return state.cards.filter(function (c) { return GRID_TASKS[c.task]; }); }

  function renderEval(main, sid) {
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
    main.appendChild(h("div", { class: "eval-title" }, h("h2", { text: card.title_ko }), conditionBadges(card)));
    var load = card.has_regress ? getJSON("data/regress/" + encodeURIComponent(card.scenario_id) + ".json") : Promise.resolve(null);
    return load.then(function (reg) { renderMaps(main, card, reg); });
  }

  function renderMaps(main, card, reg) {
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
    var maps = h("div", { class: "maps" });
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
          var btn = h("button", { type: "button", class: "cell cell-" + st + (lineBad ? " in-line-fail" : ""), disabled: !c, "aria-label": "x " + mm(x) + " mm, y " + mm(y) + " mm, " + (st === "none" ? "측정 전" : word) },
            h("span", { class: "cell-word", text: word }), c && c.video ? h("span", { class: "cell-rec", "aria-hidden": "true", title: "녹화 있음" }) : null);
          if (c) btn.addEventListener("click", function () {
            maps.querySelectorAll(".cell.sel").forEach(function (b) { b.classList.remove("sel"); });
            btn.classList.add("sel");
            showCell(panel, reg, n, conds[n], c);
          });
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
          h("span", { class: "axis-title-y", text: "y 오프셋 (mm)" }),
          gridEl,
          h("span", { class: "axis-title-x", text: "x 오프셋 (mm)" })
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
    var cause = (cell.checks || []).filter(function (c) { return c.id === "failure_event"; })[0];
    var nUnmeasured = (cell.checks || []).filter(function (c) { return c.pass === null; }).length;
    if (cause && nUnmeasured) {
      append(panel, h("p", { class: "unmeasured-note", text: withRo(FAILURE[cause.value] || cause.value) + " 시뮬이 " +
        (cause.t_s !== null && cause.t_s !== undefined ? fmt(cause.t_s, 1) + " s에 " : "") +
        "끝나 이후 검사 " + nUnmeasured + "개는 측정되지 않았습니다." }));
    }
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
      job = renderEval(main, parts[1]);
      document.title = "격자 평가 | 사고 재생 리포트";
    } else {
      renderHome(main);
      job = Promise.resolve();
      document.title = "사고 재생 리포트";
    }
    job.then(function () {
      window.scrollTo(0, 0);
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
