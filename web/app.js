/*!
 * hostping — frontend controller.
 *
 * Plain vanilla JavaScript: no frameworks, no build step, no external assets.
 * Talks to the same-origin JSON API described in SPEC.md §6:
 *   GET /api/hosts -> { generated_at, hosts_file, duration_ms, timeout_ms, summary, entries }
 *   error shape    -> { "error": "<message>", "hosts_file": "<path>" } (HTTP 500)
 */
(function () {
  'use strict';

  var ENDPOINT = '/api/hosts';
  var DEFAULT_INTERVAL_MS = 15000;
  var ALLOWED_INTERVALS = [5000, 15000, 30000, 60000];
  var REQUEST_TIMEOUT_MS = 12000;
  var COUNTDOWN_TICK_MS = 500;
  var STORAGE_KEY = 'hostping.interval_ms';
  var COLUMN_COUNT = 6;

  /* Colour implies nothing on its own: every status also has a label and a glyph. */
  var STATUS_META = {
    up: { label: 'Up', cls: 'up', glyph: '\u25CF' },
    degraded: { label: 'Degraded', cls: 'degraded', glyph: '\u25B2' },
    down: { label: 'Down', cls: 'down', glyph: '\u2715' },
    unknown: { label: 'Unknown', cls: 'unknown', glyph: '?' }
  };
  var COUNT_KEYS = ['up', 'degraded', 'down', 'unknown', 'total'];

  var state = {
    intervalMs: DEFAULT_INTERVAL_MS,
    paused: false,
    hasData: false,
    seq: 0,
    inFlight: null,
    nextRefreshAt: 0,
    refreshTimer: null,
    countdownTimer: null,
    dismissed: ''
  };

  var el = {};

  /* ------------------------------------------------------------------ *
   * Defensive helpers — the API shape is never trusted blindly.
   * ------------------------------------------------------------------ */

  function isObject(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
  }

  function toText(value) {
    return value === null || value === undefined ? '' : String(value);
  }

  function toNumber(value) {
    if (typeof value === 'number') {
      return isFinite(value) ? value : null;
    }
    if (typeof value === 'string' && value.trim() !== '') {
      var parsed = parseFloat(value);
      return isFinite(parsed) ? parsed : null;
    }
    return null;
  }

  function toCount(value) {
    var n = toNumber(value);
    if (n === null || n < 0) return null;
    return Math.round(n);
  }

  function has(object, key) {
    return !!object && Object.prototype.hasOwnProperty.call(object, key);
  }

  function statusKey(raw) {
    var key = toText(raw).trim().toLowerCase();
    return has(STATUS_META, key) ? key : 'unknown';
  }

  function setText(node, text) {
    if (node) node.textContent = text;
  }

  function setAttr(node, name, value) {
    if (node && node.setAttribute) node.setAttribute(name, value);
  }

  function on(node, type, handler) {
    if (node && node.addEventListener) node.addEventListener(type, handler);
  }

  function noop() {}

  function makeElement(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /** Non-empty trimmed strings from an array, a single string, or nothing. */
  function normalizeList(value, splitOnWhitespace) {
    var out = [];
    var push = function (item) {
      var text = toText(item).trim();
      if (!text) return;
      if (splitOnWhitespace) {
        var parts = text.split(/[\s,]+/);
        for (var i = 0; i < parts.length; i++) {
          if (parts[i]) out.push(parts[i]);
        }
      } else {
        out.push(text);
      }
    };
    if (Array.isArray(value)) {
      for (var i = 0; i < value.length; i++) push(value[i]);
    } else if (value !== null && value !== undefined) {
      push(value);
    }
    return out;
  }

  /* ---------------------------- formatting --------------------------- */

  /** Latency in ms, trimmed; null when there is nothing to show. */
  function formatLatency(value) {
    var n = toNumber(value);
    if (n === null || n < 0) return null;
    if (n < 1) return String(Math.round(n * 1000) / 1000);
    if (n < 100) return String(Math.round(n * 100) / 100);
    return String(Math.round(n));
  }

  function formatClock(value) {
    var text = toText(value).trim();
    if (!text) return null;
    var date = new Date(text);
    if (isNaN(date.getTime())) return text;
    return date.toLocaleTimeString();
  }

  function formatCountdown(ms) {
    var seconds = Math.max(0, Math.ceil(ms / 1000));
    if (seconds >= 60) {
      var minutes = Math.floor(seconds / 60);
      var rest = seconds % 60;
      return minutes + 'm ' + (rest < 10 ? '0' + rest : rest) + 's';
    }
    return seconds + 's';
  }

  function formatDuration(value) {
    var n = toNumber(value);
    if (n === null || n < 0) return null;
    if (n < 1000) return Math.round(n) + ' ms';
    return Math.round(n / 100) / 10 + ' s';
  }

  function formatStatusText(raw) {
    var key = toText(raw).trim();
    if (!key) return 'unknown';
    if (key === 'unresolved') return 'unresolved';
    return statusKey(key);
  }

  /* ------------------------------- banner ---------------------------- */

  function showError(message) {
    var text = toText(message).trim() || 'Something went wrong.';
    if (!el.banner) return;
    if (state.dismissed === text) return;
    setText(el.bannerText, text);
    el.banner.hidden = false;
  }

  function clearError() {
    state.dismissed = '';
    if (el.banner) el.banner.hidden = true;
  }

  function onDismissError() {
    state.dismissed = toText(el.bannerText && el.bannerText.textContent);
    if (el.banner) el.banner.hidden = true;
  }

  function announce(message) {
    setText(el.live, message);
  }

  function hideLoading() {
    if (el.loading) el.loading.hidden = true;
  }

  function startBusy() {
    if (el.table) setAttr(el.table, 'aria-busy', 'true');
  }

  function endBusy() {
    if (el.table) setAttr(el.table, 'aria-busy', 'false');
  }

  /* ------------------------------ rendering -------------------------- */

  function renderSummary(summary, entries) {
    var src = isObject(summary) ? summary : {};
    var list = Array.isArray(entries) ? entries : null;
    var counts = {};
    var i;
    for (i = 0; i < COUNT_KEYS.length; i++) {
      counts[COUNT_KEYS[i]] = toCount(src[COUNT_KEYS[i]]);
    }

    if (list) {
      var derived = { up: 0, degraded: 0, down: 0, unknown: 0 };
      for (i = 0; i < list.length; i++) {
        var entry = isObject(list[i]) ? list[i] : {};
        derived[statusKey(entry.status)] += 1;
      }
      for (i = 0; i < 4; i++) {
        var key = COUNT_KEYS[i];
        if (counts[key] === null) counts[key] = derived[key];
      }
    }
    if (counts.total === null) counts.total = list ? list.length : null;

    setText(el.stats.up, displayCount(counts.up));
    setText(el.stats.degraded, displayCount(counts.degraded));
    setText(el.stats.down, displayCount(counts.down));
    setText(el.stats.unknown, displayCount(counts.unknown));
    setText(el.stats.total, displayCount(counts.total));
    return counts;
  }

  function displayCount(value) {
    return value === null ? '\u2014' : String(value);
  }

  function renderTable(entries) {
    if (!el.tbody) return;
    var list = Array.isArray(entries) ? entries : [];
    var fragment = document.createDocumentFragment();
    if (!list.length) {
      fragment.appendChild(placeholderRow('No hosts-file entries to check.'));
    } else {
      for (var i = 0; i < list.length; i++) {
        fragment.appendChild(buildRow(isObject(list[i]) ? list[i] : {}, i));
      }
    }
    el.tbody.textContent = '';
    el.tbody.appendChild(fragment);
  }

  function placeholderRow(text) {
    var tr = makeElement('tr', 'table-placeholder');
    var td = makeElement('td', null, text);
    setAttr(td, 'colspan', String(COLUMN_COUNT));
    tr.appendChild(td);
    return tr;
  }

  function buildRow(entry, index) {
    var meta = STATUS_META[statusKey(entry.status)];
    var rawStatus = toText(entry.status).trim().toLowerCase();
    var tr = document.createElement('tr');
    tr.className = 'row row--' + meta.cls;
    setAttr(tr, 'data-status', meta.cls);
    setAttr(tr, 'data-index', String(index));

    var line = toNumber(entry.line);
    var raw = toText(entry.raw).trim();
    if (line !== null || raw) {
      tr.title = (line !== null ? 'Line ' + line + (raw ? ': ' : '') : '') + raw;
    }

    tr.appendChild(statusCell(meta, rawStatus));
    tr.appendChild(ipCell(entry));
    tr.appendChild(hostnameCell(entry));
    tr.appendChild(methodCell(entry));
    tr.appendChild(latencyCell(entry));
    tr.appendChild(warningsCell(entry));
    return tr;
  }

  function statusCell(meta, rawStatus) {
    var td = makeElement('td', 'cell cell--status');
    var badge = makeElement('span', 'badge badge--' + meta.cls);
    var glyph = makeElement('span', 'badge-glyph', meta.glyph);
    var label = makeElement('span', 'badge-label', meta.label);
    setAttr(glyph, 'aria-hidden', 'true');
    if (rawStatus && !has(STATUS_META, rawStatus)) {
      setAttr(badge, 'title', 'Unrecognised status from the API: ' + rawStatus);
    }
    badge.appendChild(glyph);
    badge.appendChild(label);
    td.appendChild(badge);
    return td;
  }

  function resultDetails(result, prefix) {
    if (!isObject(result)) return [];
    var parts = [];
    var status = toText(result.status).trim();
    var method = toText(result.method).trim();
    var latency = formatLatency(result.latency_ms);
    if (status || method) {
      parts.push(prefix + ' check: ' + (status || 'unknown') + (method ? ' via ' + method : ''));
    }
    if (latency !== null) parts.push(latency + ' ms');
    if (result.error) parts.push(toText(result.error).trim());
    return parts;
  }

  function ipCell(entry) {
    var td = makeElement('td', 'cell cell--ip');
    var ip = toText(entry.ip).trim();
    td.appendChild(makeElement('code', 'mono', ip || '\u2014'));
    var details = resultDetails(entry.ip_result, 'IP');
    if (details.length) td.title = details.join(' \u00B7 ');
    return td;
  }

  function hostnameCell(entry) {
    var td = makeElement('td', 'cell cell--host');
    var hostname = toText(entry.hostname).trim();
    td.appendChild(makeElement('code', 'mono', hostname || '\u2014'));

    var aliases = normalizeList(entry.aliases, true);
    if (aliases.length) {
      var aliasNode = makeElement('span', 'aliases', '(' + aliases.join(', ') + ')');
      setAttr(aliasNode, 'title', 'Alias' + (aliases.length > 1 ? 'es' : '') + ': ' + aliases.join(', '));
      td.appendChild(aliasNode);
    }

    var details = resultDetails(entry.hostname_result, 'hostname');
    if (details.length) td.title = details.join(' \u00B7 ');
    return td;
  }

  function methodCell(entry) {
    var td = makeElement('td', 'cell cell--method');
    var ipResult = isObject(entry.ip_result) ? entry.ip_result : null;
    var hostResult = isObject(entry.hostname_result) ? entry.hostname_result : null;
    var method = toText(
      (hostResult && hostResult.method) || (ipResult && ipResult.method) || entry.method
    ).trim().toLowerCase();

    var known = method === 'icmp' || method === 'tcp';
    var chip = makeElement('code', 'mono method method--' + (known ? method : 'unknown'), method || '\u2014');

    var tips = [];
    if (ipResult && toText(ipResult.method).trim()) tips.push('IP: ' + toText(ipResult.method).trim());
    if (hostResult && toText(hostResult.method).trim()) {
      tips.push('hostname: ' + toText(hostResult.method).trim());
    }
    if (tips.length) setAttr(td, 'title', tips.join(' \u00B7 '));

    td.appendChild(chip);
    return td;
  }

  function latencyCell(entry) {
    var td = makeElement('td', 'cell cell--latency col-num');
    var ipResult = isObject(entry.ip_result) ? entry.ip_result : null;
    var hostResult = isObject(entry.hostname_result) ? entry.hostname_result : null;

    var value = hostResult ? hostResult.latency_ms : undefined;
    if (value === null || value === undefined) {
      value = ipResult ? ipResult.latency_ms : undefined;
    }
    if (value === null || value === undefined) {
      value = entry.latency_ms;
    }

    var latency = formatLatency(value);
    var node = makeElement('span', latency === null ? 'muted latency' : 'mono latency', latency === null ? '\u2014' : latency);

    if (latency === null) {
      setAttr(node, 'title', 'No latency reported for this entry.');
    } else {
      var tips = [];
      var hostLatency = formatLatency(hostResult ? hostResult.latency_ms : null);
      var ipLatency = formatLatency(ipResult ? ipResult.latency_ms : null);
      if (hostLatency !== null) tips.push('hostname: ' + hostLatency + ' ms');
      if (ipLatency !== null) tips.push('IP: ' + ipLatency + ' ms');
      if (tips.length) setAttr(node, 'title', tips.join(' \u00B7 '));
    }

    td.appendChild(node);
    return td;
  }

  function warningsCell(entry) {
    var td = makeElement('td', 'cell cell--warnings');
    var warnings = normalizeList(entry.warnings, false);
    if (!warnings.length) {
      td.appendChild(makeElement('span', 'muted', '\u2014'));
      return td;
    }
    var list = makeElement('ul', 'warnings');
    for (var i = 0; i < warnings.length; i++) {
      var item = makeElement('li', 'warning');
      var icon = makeElement('span', 'warning-icon', '\u26A0');
      setAttr(icon, 'aria-hidden', 'true');
      item.appendChild(icon);
      item.appendChild(makeElement('span', 'warning-text', warnings[i]));
      list.appendChild(item);
    }
    td.appendChild(list);
    return td;
  }

  function renderMeta(body, entryCount) {
    var generatedAt = toText(body.generated_at).trim();
    var clock = formatClock(generatedAt);
    setText(el.lastUpdated, clock || '\u2014');
    if (el.lastUpdated) {
      if (generatedAt) setAttr(el.lastUpdated, 'title', generatedAt + ' (UTC)');
      else el.lastUpdated.removeAttribute('title');
    }

    var parts = [];
    var hostsFile = toText(body.hosts_file).trim();
    if (hostsFile) parts.push('hosts file ' + hostsFile);
    parts.push(entryCount + (entryCount === 1 ? ' entry' : ' entries'));
    var duration = formatDuration(body.duration_ms);
    if (duration !== null) parts.push('scan took ' + duration);
    var timeout = formatDuration(body.timeout_ms);
    if (timeout !== null) parts.push('per-check timeout ' + timeout);
    setText(el.scanMeta, parts.join(' \u00B7 '));
  }

  /** Apply one successful payload to the DOM and return the resolved counts. */
  function applyPayload(body) {
    var entries = Array.isArray(body.entries) ? body.entries : [];
    var counts = renderSummary(body.summary, entries);
    renderTable(entries);
    renderMeta(body, entries.length);
    state.hasData = true;
    return counts;
  }

  function announceUpdate(counts, entryCount) {
    if (counts.up === null || counts.down === null || counts.degraded === null || counts.unknown === null) {
      announce('Updated. ' + entryCount + (entryCount === 1 ? ' entry.' : ' entries.'));
      return;
    }
    announce(
      'Updated. ' + counts.up + ' up, ' + counts.degraded + ' degraded, ' +
      counts.down + ' down, ' + counts.unknown + ' unknown of ' +
      (counts.total === null ? entryCount : counts.total) + ' hosts.'
    );
  }

  /* --------------------------- status readout ------------------------ */

  function updateCountdown() {
    var node = el.countdown;
    if (!node) return;
    if (state.paused) {
      setText(node, 'paused');
      return;
    }
    if (!state.nextRefreshAt) {
      setText(node, 'refreshing\u2026');
      return;
    }
    var remaining = state.nextRefreshAt - Date.now();
    setText(node, remaining <= 0 ? 'now' : 'in ' + formatCountdown(remaining));
  }

  /* ------------------------------- timers ---------------------------- */

  function scheduleRefresh() {
    if (state.refreshTimer !== null) {
      clearTimeout(state.refreshTimer);
      state.refreshTimer = null;
    }
    if (state.paused) {
      state.nextRefreshAt = 0;
      updateCountdown();
      return;
    }
    state.nextRefreshAt = Date.now() + state.intervalMs;
    state.refreshTimer = setTimeout(onRefreshTimer, state.intervalMs);
    updateCountdown();
  }

  function onRefreshTimer() {
    state.refreshTimer = null;
    state.nextRefreshAt = 0;
    updateCountdown();
    runRefresh('auto');
  }

  /* ----------------------------- networking -------------------------- */

  function isAbortError(error) {
    return !!error && (error.name === 'AbortError' || error.code === 20);
  }

  function errorText(error) {
    if (!error) return 'Could not reach the hostping API.';
    if (error.name === 'TypeError') {
      return 'Could not reach the hostping API. Is the server still running?';
    }
    return toText(error.userMessage || error.message).trim() ||
      'Could not reach the hostping API.';
  }

  async function requestHosts(controller) {
    var response = await fetch(ENDPOINT, {
      method: 'GET',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
      signal: controller ? controller.signal : undefined
    });

    var text = '';
    try {
      text = await response.text();
    } catch (readError) {
      text = '';
    }

    var body = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch (parseError) {
        body = null;
      }
    }

    if (!response.ok) {
      var detail = isObject(body) && body.error ? toText(body.error).trim() : 'HTTP ' + response.status;
      var where = isObject(body) && body.hosts_file ? ' (' + toText(body.hosts_file).trim() + ')' : '';
      var apiError = new Error('The API reported an error: ' + detail + where);
      apiError.userMessage = apiError.message;
      throw apiError;
    }

    if (!isObject(body)) {
      var shapeError = new Error('Unexpected response from ' + ENDPOINT + ' \u2014 expected a JSON object.');
      shapeError.userMessage = shapeError.message;
      throw shapeError;
    }
    return body;
  }

  /**
   * Fetch and render.
   *  - 'auto'   : skipped entirely while a request is in flight (never overlaps)
   *  - anything else (initial / manual / resume): supersedes the in-flight request
   */
  async function refresh(reason) {
    if (state.inFlight) {
      if (reason === 'auto') return;
      try {
        if (state.inFlight.controller) state.inFlight.controller.abort();
      } catch (abortError) {
        /* ignore */
      }
    }

    var token = ++state.seq;
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var record = { token: token, controller: controller, timedOut: false, timer: null };
    if (controller) {
      record.timer = setTimeout(function () {
        record.timedOut = true;
        try {
          controller.abort();
        } catch (abortError) {
          /* ignore */
        }
      }, REQUEST_TIMEOUT_MS);
    }
    state.inFlight = record;
    startBusy();

    try {
      var body = await requestHosts(controller);
      if (token !== state.seq) return; // a newer request owns the UI now
      endBusy();
      clearError();
      hideLoading();
      var counts = applyPayload(body);
      announceUpdate(counts, Array.isArray(body.entries) ? body.entries.length : 0);
    } catch (error) {
      if (token !== state.seq) return;
      if (isAbortError(error) && !record.timedOut) return; // superseded by a newer request
      endBusy();
      hideLoading();
      var message = record.timedOut
        ? 'The request to ' + ENDPOINT + ' timed out after ' + Math.round(REQUEST_TIMEOUT_MS / 1000) + ' s.'
        : errorText(error);
      // Keep the last known data on screen; only report the failure.
      showError(message);
      announce('Update failed. ' + message + (state.hasData ? ' Showing the previous results.' : ''));
    } finally {
      if (record.timer !== null) clearTimeout(record.timer);
      if (state.inFlight && state.inFlight.token === token) state.inFlight = null;
    }
  }

  function runRefresh(reason) {
    return refresh(reason).catch(noop).then(scheduleRefresh);
  }

  /* ------------------------------- events ---------------------------- */

  function onIntervalChange() {
    var value = toNumber(el.interval ? el.interval.value : null);
    if (value === null || ALLOWED_INTERVALS.indexOf(value) === -1) {
      value = DEFAULT_INTERVAL_MS;
      if (el.interval) el.interval.value = String(value);
    }
    state.intervalMs = value;
    storeInterval(value);
    scheduleRefresh();
    announce('Refresh interval set to ' + formatCountdown(value) + '.');
  }

  function onTogglePause() {
    state.paused = !state.paused;
    if (el.pause) {
      el.pause.textContent = state.paused ? 'Resume' : 'Pause';
      setAttr(el.pause, 'aria-pressed', state.paused ? 'true' : 'false');
      setAttr(el.pause, 'title', state.paused ? 'Resume automatic refresh' : 'Pause automatic refresh');
    }
    scheduleRefresh();
    announce(state.paused
      ? 'Automatic refresh paused.'
      : 'Automatic refresh resumed, every ' + formatCountdown(state.intervalMs) + '.');
    if (!state.paused && !state.hasData) runRefresh('resume');
  }

  function onManualRefresh() {
    runRefresh('manual');
  }

  /* --------------------------- persistence --------------------------- */

  function readStoredInterval() {
    try {
      if (!window.localStorage) return DEFAULT_INTERVAL_MS;
      var value = toNumber(window.localStorage.getItem(STORAGE_KEY));
      if (value !== null && ALLOWED_INTERVALS.indexOf(value) !== -1) return value;
    } catch (storageError) {
      /* private mode / disabled storage — fall back to the default */
    }
    return DEFAULT_INTERVAL_MS;
  }

  function storeInterval(value) {
    try {
      if (window.localStorage) window.localStorage.setItem(STORAGE_KEY, String(value));
    } catch (storageError) {
      /* not fatal */
    }
  }

  /* -------------------------------- init ------------------------------ */

  function collectElements() {
    el = {
      banner: document.getElementById('banner'),
      bannerText: document.getElementById('banner-text'),
      bannerDismiss: document.getElementById('banner-dismiss'),
      loading: document.getElementById('loading'),
      tbody: document.getElementById('hosts-body'),
      table: document.getElementById('hosts-table'),
      interval: document.getElementById('interval-select'),
      pause: document.getElementById('pause-btn'),
      refresh: document.getElementById('refresh-btn'),
      lastUpdated: document.getElementById('last-updated'),
      countdown: document.getElementById('countdown'),
      scanMeta: document.getElementById('scan-meta'),
      live: document.getElementById('live'),
      stats: {
        up: document.getElementById('stat-up'),
        degraded: document.getElementById('stat-degraded'),
        down: document.getElementById('stat-down'),
        unknown: document.getElementById('stat-unknown'),
        total: document.getElementById('stat-total')
      }
    };
  }

  function init() {
    collectElements();
    if (!el.tbody) return; // page shell missing — nothing sensible to do

    if (typeof fetch !== 'function') {
      showError('This browser does not support fetch(), which hostping needs.');
      hideLoading();
      return;
    }

    state.intervalMs = readStoredInterval();
    if (el.interval) el.interval.value = String(state.intervalMs);

    on(el.interval, 'change', onIntervalChange);
    on(el.pause, 'click', onTogglePause);
    on(el.refresh, 'click', onManualRefresh);
    on(el.bannerDismiss, 'click', onDismissError);

    state.countdownTimer = setInterval(updateCountdown, COUNTDOWN_TICK_MS);
    scheduleRefresh();
    runRefresh('initial');
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', init);
    } else {
      init();
    }
  }
})();
