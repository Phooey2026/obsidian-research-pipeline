/*
 * tts_player.js — "Listen" player for the Obsidian Capital dashboard
 * ------------------------------------------------------------------
 * Loaded by dashboard.html (one <script src="tts_player.js"> tag added by
 * weekly_research.generate_dashboard).  It watches for the OUTLOOK and JANSKY
 * panels, and puts a player bar at the top of the Atlas briefing and Jansky's
 * executive briefing.  All the work happens in tts_server.py (port 8646);
 * this file only talks to it, so editing it never needs a dashboard rebuild
 * or a Jansky re-run — just reload the page.
 *
 * Override the service address with:  window.TTS_API = "http://host:8646"
 */
(function () {
  'use strict';

  var API = (window.TTS_API ||
             (location.protocol === 'https:' ? 'http:' : location.protocol) + '//' +
             (location.hostname || 'localhost') + ':8646').replace(/\/$/, '');

  var TARGETS = [
    { report: 'atlas',  find: function () { return document.getElementById('outlook-prose-body'); } },
    { report: 'jansky', find: function () { return document.getElementById('jansky-exec-body'); } }
  ];

  // ── styles ────────────────────────────────────────────────────────────────
  var css = [
    '.tts-bar{position:relative;display:flex;align-items:center;gap:12px;margin:4px 0 16px;',
    ' padding:10px 12px;background:var(--surface,#111827);border:1px solid var(--border,#1f2937);',
    ' border-radius:6px;font-family:inherit}',
    '.tts-btn{flex:0 0 auto;min-width:128px;padding:8px 14px;border-radius:4px;cursor:pointer;',
    ' font:600 12px/1 inherit;letter-spacing:.8px;color:var(--accent2,#f59e0b);',
    ' background:transparent;border:1px solid var(--accent2,#f59e0b)}',
    '.tts-btn:hover:not(:disabled){background:rgba(245,158,11,.12)}',
    '.tts-btn:disabled{opacity:.45;cursor:default}',
    '.tts-mid{flex:1 1 auto;min-width:0}',
    '.tts-prog{height:6px;border-radius:3px;background:var(--border,#1f2937);overflow:hidden}',
    '.tts-fill{height:100%;width:0;background:var(--accent2,#f59e0b);transition:width .5s ease}',
    '.tts-fill.indet{width:35%;animation:ttsslide 1.2s ease-in-out infinite alternate}',
    '@keyframes ttsslide{from{margin-left:0}to{margin-left:65%}}',
    '.tts-msg{margin-top:6px;font-size:11px;letter-spacing:.3px;color:var(--muted,#6b7280);',
    ' white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    '.tts-msg.err{color:#ef4444}',
    '.tts-audio{width:100%;height:34px;display:block}',
    '.tts-gear{flex:0 0 auto;background:transparent;border:1px solid var(--border,#1f2937);',
    ' color:var(--muted,#9ca3af);border-radius:4px;padding:6px 9px;cursor:pointer;font-size:14px}',
    '.tts-gear:hover{color:var(--text,#e5e7eb)}',
    '.tts-pop{position:absolute;right:8px;top:100%;margin-top:6px;z-index:50;width:min(320px,92vw);',
    ' padding:14px;background:var(--surface,#111827);border:1px solid var(--border,#374151);',
    ' border-radius:6px;box-shadow:0 8px 24px rgba(0,0,0,.5);font-size:12px;color:var(--text,#e5e7eb)}',
    '.tts-pop label{display:block;margin-bottom:6px;color:var(--muted,#9ca3af);',
    ' text-transform:uppercase;letter-spacing:1.2px;font-size:10px}',
    '.tts-pop select{width:100%;padding:7px;background:var(--bg,#0b0f19);color:inherit;',
    ' border:1px solid var(--border,#374151);border-radius:4px;font-size:13px}',
    '.tts-row{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}',
    '.tts-row button{flex:1 1 auto;padding:7px 10px;border-radius:4px;cursor:pointer;font-size:12px;',
    ' background:transparent;color:inherit;border:1px solid var(--border,#374151)}',
    '.tts-row button:hover:not(:disabled){border-color:var(--accent2,#f59e0b)}',
    '.tts-row button:disabled{opacity:.45;cursor:default}',
    '.tts-note{margin-top:8px;color:var(--muted,#6b7280);font-size:11px;line-height:1.4}',
    '@media(max-width:600px){.tts-bar{flex-wrap:wrap}.tts-mid{flex-basis:100%;order:3}}'
  ].join('');
  var style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  // ── helpers ───────────────────────────────────────────────────────────────
  function api(path, opts) {
    var ctl = ('AbortController' in window) ? new AbortController() : null;
    var timer = ctl ? setTimeout(function () { ctl.abort(); }, 15000) : null;
    var o = opts || {};
    if (ctl) o.signal = ctl.signal;
    return fetch(API + path, o).then(function (r) {
      if (timer) clearTimeout(timer);
      return r.json();
    }, function (e) {
      if (timer) clearTimeout(timer);
      throw e;
    });
  }
  function post(path, obj) {
    return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                       body: JSON.stringify(obj) });
  }
  function mmss(s) {
    if (!s && s !== 0) return '';
    var m = Math.floor(s / 60), r = Math.round(s % 60);
    return m + ':' + (r < 10 ? '0' : '') + r;
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }

  // ── one player per report ────────────────────────────────────────────────
  function buildPlayer(report) {
    var st = null, pollTimer = null, audioSrc = '';

    var bar   = el('div', 'tts-bar');
    var btn   = el('button', 'tts-btn', 'Checking…');
    var mid   = el('div', 'tts-mid');
    var prog  = el('div', 'tts-prog');
    var fill  = el('div', 'tts-fill');
    var msg   = el('div', 'tts-msg', '');
    var audio = el('audio', 'tts-audio');
    var gear  = el('button', 'tts-gear', '⚙');
    var pop   = el('div', 'tts-pop');
    btn.type = gear.type = 'button';
    gear.title = 'Voice & options';
    audio.controls = true;
    audio.preload = 'metadata';
    audio.style.display = 'none';
    prog.appendChild(fill);
    mid.appendChild(prog); mid.appendChild(audio); mid.appendChild(msg);
    bar.appendChild(btn); bar.appendChild(mid); bar.appendChild(gear);
    pop.style.display = 'none';
    bar.appendChild(pop);

    // popover contents
    var vLabel = el('label', '', 'Voice');
    var vSel   = el('select');
    var vRow   = el('div', 'tts-row');
    var fetchB = el('button', '', 'Fetch voices');
    var saveB  = el('button', '', 'Use this voice');
    var regenB = el('button', '', 'Regenerate audio');
    var vNote  = el('div', 'tts-note', '');
    fetchB.type = saveB.type = regenB.type = 'button';
    vRow.appendChild(fetchB); vRow.appendChild(saveB);
    var rRow = el('div', 'tts-row'); rRow.appendChild(regenB);
    pop.appendChild(vLabel); pop.appendChild(vSel); pop.appendChild(vRow);
    pop.appendChild(rRow); pop.appendChild(vNote);

    function setMsg(t, isErr) { msg.textContent = t || ''; msg.className = 'tts-msg' + (isErr ? ' err' : ''); }
    function showProgress(frac, indet) {
      prog.style.display = '';
      audio.style.display = 'none';
      fill.className = 'tts-fill' + (indet ? ' indet' : '');
      fill.style.width = indet ? '' : Math.max(2, Math.round(frac * 100)) + '%';
    }
    function showAudio() { prog.style.display = 'none'; audio.style.display = 'block'; }
    function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
    function startPoll() { if (!pollTimer) pollTimer = setInterval(refresh, 1500); }

    function costText(s) {
      return (s.est_cost === null || s.est_cost === undefined)
        ? 'cost estimate unavailable'
        : 'rough cost ≈ $' + s.est_cost.toFixed(2);
    }

    function render(s) {
      st = s;
      btn.disabled = false;
      switch (s.state) {
        case 'idle':
          btn.textContent = '🔊 Generate audio';
          showProgress(0, false); fill.style.width = '0';
          setMsg('~' + s.est_minutes + ' min · ' + s.words + ' words · ' + costText(s) +
                 ' · voice ' + s.voice);
          stopPoll(); break;
        case 'running':
          btn.textContent = 'Generating…'; btn.disabled = true;
          showProgress(s.total ? s.done / s.total : 0, !s.done);
          setMsg((s.message || 'Working…') + (s.total ? '  (' + s.done + '/' + s.total + ' done)' : ''));
          startPoll(); break;
        case 'ready':
          stopPoll();
          if (audioSrc !== s.audio_url) {
            audioSrc = s.audio_url;
            audio.src = API + s.audio_url;
          }
          showAudio();
          btn.textContent = audio.paused ? '▶ Play' : '⏸ Pause';
          setMsg('voice ' + s.voice + (s.duration_s ? ' · ' + mmss(s.duration_s) : '') +
                 (s.cost_usd !== null && s.cost_usd !== undefined ? ' · cost $' + s.cost_usd.toFixed(3) : ''));
          break;
        case 'error':
          stopPoll();
          btn.textContent = '↻ Retry';
          showProgress(s.total ? s.done / s.total : 0, false);
          setMsg(s.message || 'Failed', true); break;
        default:                         // unavailable / unconfigured
          stopPoll();
          btn.textContent = 'Not available'; btn.disabled = true;
          showProgress(0, false); fill.style.width = '0';
          setMsg(s.message || 'Not available', true);
      }
    }

    function offline() {
      stopPoll();
      btn.textContent = 'Offline'; btn.disabled = true;
      showProgress(0, false); fill.style.width = '0';
      setMsg('TTS service not reachable at ' + API + ' — is tts-server running?', true);
    }

    function refresh() {
      return api('/api/tts/status?report=' + report).then(render, function () { if (!st) offline(); });
    }

    function generate(force) {
      btn.disabled = true;
      return post('/api/tts/generate', { report: report, force: !!force }).then(render, offline);
    }

    function confirmText(s, again) {
      return (again ? 'Regenerate' : 'Generate') + ' spoken audio for:\n' + s.label + '\n\n' +
             '≈ ' + s.est_minutes + ' min of audio (' + s.words + ' words)\n' +
             costText(s) + '\nVoice: ' + s.voice + '\n\nContinue?';
    }

    btn.addEventListener('click', function () {
      if (!st) return;
      if (st.state === 'idle') {
        if (confirm(confirmText(st, false))) generate(false);
      } else if (st.state === 'error') {
        generate(false);                           // resumes finished parts
      } else if (st.state === 'ready') {
        if (audio.paused) { var p = audio.play(); if (p && p.catch) p.catch(function () {}); }
        else audio.pause();
      }
    });
    audio.addEventListener('play',  function () { if (st && st.state === 'ready') btn.textContent = '⏸ Pause'; });
    audio.addEventListener('pause', function () { if (st && st.state === 'ready') btn.textContent = '▶ Play'; });
    audio.addEventListener('ended', function () { if (st && st.state === 'ready') btn.textContent = '▶ Play'; });

    // ── voice popover ───────────────────────────────────────────────────────
    function fillVoices(d) {
      vSel.innerHTML = '';
      (d.voices || []).forEach(function (v) {
        var o = document.createElement('option');
        o.value = v; o.textContent = v + (v === d.current ? '  (current)' : '');
        if (v === d.current) o.selected = true;
        vSel.appendChild(o);
      });
      vNote.textContent = d.source === 'openrouter' ? 'Voices from OpenRouter for ' + d.model
        : d.source === 'fallback' ? 'OpenRouter returned no voice list — showing the built-in Gemini voices.'
        : 'No voice list available.';
    }
    function loadVoices(refresh) {
      fetchB.disabled = true;
      return api('/api/tts/voices?report=' + report + (refresh ? '&refresh=1' : ''))
        .then(fillVoices, function () { vNote.textContent = 'Could not reach the TTS service.'; })
        .then(function () { fetchB.disabled = false; });
    }
    gear.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = pop.style.display === 'none';
      pop.style.display = open ? 'block' : 'none';
      if (open) loadVoices(false);
    });
    pop.addEventListener('click', function (e) { e.stopPropagation(); });
    document.addEventListener('click', function () { pop.style.display = 'none'; });
    fetchB.addEventListener('click', function () { loadVoices(true); });
    saveB.addEventListener('click', function () {
      var v = vSel.value;
      if (!v) return;
      saveB.disabled = true;
      audio.pause();
      post('/api/tts/voice', { report: report, voice: v }).then(function (s) {
        audioSrc = ''; render(s); pop.style.display = 'none';
      }, function () { vNote.textContent = 'Could not save the voice.'; })
        .then(function () { saveB.disabled = false; });
    });
    regenB.addEventListener('click', function () {
      if (!st || !st.words) return;
      if (confirm(confirmText(st, true))) { audio.pause(); audioSrc = ''; pop.style.display = 'none'; generate(true); }
    });

    document.addEventListener('visibilitychange', function () { if (!document.hidden) refresh(); });
    refresh();
    return bar;
  }

  // ── inject once the panels exist ─────────────────────────────────────────
  var done = {};
  function tryInject() {
    var remaining = 0;
    TARGETS.forEach(function (t) {
      if (done[t.report]) return;
      var node = t.find();
      if (node && node.parentNode) {
        node.parentNode.insertBefore(buildPlayer(t.report), node);
        done[t.report] = true;
      } else {
        remaining++;
      }
    });
    return remaining === 0;
  }
  if (!tryInject()) {
    var obs = new MutationObserver(function () { if (tryInject()) obs.disconnect(); });
    obs.observe(document.documentElement, { childList: true, subtree: true });
    setTimeout(function () { obs.disconnect(); }, 120000);
  }
})();
