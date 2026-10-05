/*! nodemeet-ui.js - ready-made, fully brandable meeting UI on top of nodemeet.js (PolyForm Noncommercial 1.0.0) */
(function (global) {
  'use strict';
  const NM = global.NodeMeet;
  if (!NM || !NM.Client) { console.error('[nodemeet] load nodemeet.js before nodemeet-ui.js'); return; }

  const P = (d) => '<svg viewBox="0 0 24 24" aria-hidden="true">' + d + '</svg>';
  const ICONS = {
    mic: P('<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/>'),
    micOff: P('<line x1="2" y1="2" x2="22" y2="22"/><path d="M18.9 13.3A7 7 0 0 0 19 12v-2"/><path d="M5 10v2a7 7 0 0 0 12 5"/><path d="M15 9.3V5a3 3 0 0 0-5.7-1.3"/><path d="M9 9v3a3 3 0 0 0 5.1 2.1"/><line x1="12" y1="19" x2="12" y2="22"/>'),
    camera: P('<path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2"/>'),
    cameraOff: P('<line x1="2" y1="2" x2="22" y2="22"/><path d="M16 16v1a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2m5 0h4a2 2 0 0 1 2 2v3.3l7-4.3v12"/>'),
    screen: P('<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>'),
    hand: P('<path d="M18 11V6a2 2 0 0 0-4 0v5"/><path d="M14 10V4a2 2 0 0 0-4 0v6"/><path d="M10 10.5V6a2 2 0 0 0-4 0v8"/><path d="M18 8a2 2 0 1 1 4 0v6a8 8 0 0 1-8 8h-2c-2.8 0-4.5-.9-6-2.4l-3.6-3.6a2 2 0 0 1 2.8-2.8L7 15"/>'),
    chat: P('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
    people: P('<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.9"/><path d="M16 3.1a4 4 0 0 1 0 7.8"/>'),
    leave: P('<path d="M10.7 13.3a16 16 0 0 0 3.4 2.6l1.3-1.3a2 2 0 0 1 2.1-.4c.8.3 1.7.5 2.6.6A2 2 0 0 1 22 16.9v3a2 2 0 0 1-2.2 2A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1.9.3 1.8.6 2.6a2 2 0 0 1-.4 2.1L8 9.7"/><line x1="23" y1="1" x2="1" y2="23"/>'),
    settings: P('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
    layout: P('<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>'),
    reactions: P('<circle cx="12" cy="12" r="10"/><path d="M8 14s1.5 2 4 2 4-2 4-2"/><line x1="9" y1="9" x2="9.01" y2="9"/><line x1="15" y1="9" x2="15.01" y2="9"/>'),
    more: P('<circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/>'),
    whiteboard: P('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'),
    captions: P('<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M10 10.5a2 2 0 1 0 0 3M17 10.5a2 2 0 1 0 0 3"/>'),
  };

  // Every visible string. Override any of them with branding.strings (or per language).
  const STRINGS = {
    mic: 'Microphone', camera: 'Camera', screen: 'Share', hand: 'Raise hand', reactions: 'React', chat: 'Chat',
    people: 'People', layout: 'Layout', settings: 'Settings', more: 'More', leave: 'Leave',
    ready: 'Ready to join?', your_name: 'Your name', join_now: 'Join now', mic_on: 'Microphone on', cam_on: 'Camera on',
    could_not_join: 'Could not join: {error}', waiting_title: 'Waiting for the host', waiting_text: "You'll join as soon as someone lets you in.",
    denied: 'The host did not let you in', joined: '{name} joined', left_msg: '{name} left', you: 'you',
    send: 'Send', message_ph: 'Send a message', everyone: 'Everyone', private_to: 'Private to {name}', private: 'private',
    chat_off: 'Chat is turned off', pin: 'Pin', unpin: 'Unpin', delete: 'Delete', pinned: 'Pinned',
    in_meeting: 'In the meeting', waiting_room: 'Waiting room', admit: 'Admit', deny: 'Deny', admit_all: 'Admit all',
    mute: 'Mute', ask_unmute: 'Ask to unmute', allow_unmute: 'Allow to unmute', video_off: 'Turn off camera', allow_video: 'Allow camera',
    stop_screen: 'Stop screen share', lower_hand: 'Lower hand', spotlight: 'Spotlight for everyone', unspotlight: 'Remove spotlight',
    rename: 'Rename', set_role: 'Change role', invite_stage: 'Invite to stage', remove_stage: 'Remove from stage',
    message_private: 'Send private message', kick: 'Remove', ban: 'Remove and block', confirm_kick: 'Remove {name} from the meeting?',
    confirm_ban: 'Remove {name} and block them from rejoining?', mute_all: 'Mute everyone', lower_all: 'Lower all hands',
    lock: 'Lock meeting', unlock: 'Unlock meeting', lobby_on: 'Turn on waiting room', lobby_off: 'Turn off waiting room',
    chat_enable: 'Turn chat on', chat_disable: 'Turn chat off', end_meeting: 'End meeting for everyone', confirm_end: 'End the meeting for everyone?',
    rename_self: 'Change my name', fullscreen: 'Full screen', pip: 'Picture in picture',
    grid: 'Grid', speaker: 'Speaker', sidebar: 'Sidebar',
    settings_title: 'Settings', cam_label: 'Camera', mic_label: 'Microphone', speaker_label: 'Speaker', quality: 'Video quality',
    q_auto: 'Automatic', q_low: 'Low (360p)', q_medium: 'Medium (540p)', q_high: 'High (720p)', q_hd: 'Full HD (1080p)',
    echo: 'Echo cancellation', noise: 'Noise suppression', agc: 'Automatic gain', blur: 'Blur background', mirror: 'Mirror my video',
    hide_self: 'Hide my own video', test_speaker: 'Test speaker', close: 'Close', save: 'Save', default_dev: 'System default',
    muted_by: 'You were muted by {by}', video_off_by: '{by} turned off your camera', screen_stopped: '{by} stopped your screen share',
    unmute_req: '{by} asks you to unmute', unmute_btn: 'Unmute', allowed_unmute: 'You can unmute now', allowed_video: 'You can turn your camera on now',
    kicked: 'You were removed from the meeting by {by}', banned: 'You were removed from this meeting', ended: 'The meeting has ended',
    left: 'You left the meeting', disconnected: 'Disconnected', rejoin: 'Rejoin', reconnecting: 'Reconnecting… ({n})',
    media_error: 'Camera/mic unavailable: {error}', can_speak: 'You can now speak and share video', watch_only: 'You are now watching only',
    role_changed: 'Your role is now {role}', switched_sfu: 'Switched to server relay for a bigger room', switched_p2p: 'Switched to direct peer-to-peer',
    admit_always: 'Always admit', block: 'Block', blocked: 'Blocked', unban: 'Unblock',
    confirm_block: 'Block {name}? They will not be able to join this meeting again.',
    ban_attempt: '{name} (blocked) tried to rejoin', banned_rejoin: "You were blocked from this meeting and can't rejoin. Ask the host if this is a mistake.",
    add_join_list: 'Add to join list (skip waiting room next time)', remove_join_list: 'Remove from join list', on_list: 'on join list',
    polls: 'Polls', qa: 'Q&A', breakouts: 'Breakouts', whiteboard: 'Whiteboard', captions: 'Captions',
    new_poll: 'New poll', question_ph: 'Question', options_ph: 'One option per line', multiple: 'Allow several answers',
    anonymous: 'Anonymous', create: 'Create', vote: 'Vote', close_poll: 'Close poll', closed: 'Closed', votes: '{n} votes',
    ask_ph: 'Ask a question', ask: 'Ask', ask_anon: 'Ask anonymously', answered: 'Answered', answer: 'Answer',
    mark_answered: 'Mark answered', highlight: 'Highlight', dismiss: 'Dismiss', answer_ph: 'Your answer',
    rooms_count: 'Rooms', auto_assign: 'Assign automatically', let_choose: 'Let people choose a room', duration: 'Minutes (optional)',
    create_rooms: 'Create rooms', open_rooms: 'Open rooms', close_rooms: 'Close rooms', broadcast: 'Message all rooms',
    join_room: 'Join', move_to: 'Move to…', main_room: 'Main room', assigned: "You've been moved to {name}", go_now: 'Go now',
    closing_in: 'Breakout rooms close in {n}s', moving: 'Moving to {name}…', announcement: '📢 {by}: {text}',
    rec: 'REC', rec_start: 'Start recording', rec_stop: 'Stop recording', rec_started: '{by} started recording this meeting',
    rec_stopped: 'Recording stopped', rec_saved: 'Recording saved', cc_on: 'Turn on live captions', cc_off: 'Turn off live captions',
    transcript: 'Download transcript', lobby_msg: 'Waiting room message', lobby_until_host: 'Admit everyone once a host is here',
    lobby_manual: 'Admit people manually', position: "You're #{n} in line", knock: '{name} is waiting to join',
    pen: 'Pen', eraser: 'Eraser', undo: 'Undo', clear: 'Clear', ban_device: 'Block (this device too)',
    spotlighted: '{name} is in the spotlight', powered_by: 'Powered by {name}', locked: 'Meeting locked', unlocked: 'Meeting unlocked',
  };

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const initials = (n) => (String(n || '?').trim().split(/\s+/).map((w) => w[0]).join('').slice(0, 2) || '?').toUpperCase();
  const el = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const MENU_ACTS = { more: 1, reactions: 1, layout: 1 };
  const DEFAULT_TOOLBAR = ['mic', 'camera', 'screen', 'hand', 'reactions', 'chat', 'people', 'whiteboard', 'captions', 'layout', 'settings', 'more', 'leave'];
  const DEFAULT_FEATURES = { prejoin: true, device_preview: true, chat: true, people: true, reactions: true, settings: true,
    layout_switch: true, fullscreen: true, private_chat: true, notifications: true, sounds: true, show_names: true,
    show_role_badges: true, self_view: true, picture_in_picture: true, polls: true, qa: true,
    whiteboard: true, breakouts: true, captions: true, recording: true };

  /** Apply a branding object to a root element (CSS variables + button style). */
  function applyBranding(root, b) {
    b = b || {};
    const dark = b.colors || {}, light = b.light_colors || {};
    const prefersLight = global.matchMedia && global.matchMedia('(prefers-color-scheme: light)').matches;
    const palette = b.theme === 'light' || (b.theme === 'auto' && prefersLight) ? Object.assign({}, dark, light) : dark;
    Object.keys(palette).forEach((k) => root.style.setProperty('--nm-' + k.replace(/_/g, '-'), palette[k]));
    if (b.font_family) root.style.setProperty('--nm-font', b.font_family);
    if (b.heading_font_family) root.style.setProperty('--nm-heading-font', b.heading_font_family);
    if (b.font_size) root.style.setProperty('--nm-font-size', b.font_size + 'px');
    if (b.radius != null) root.style.setProperty('--nm-radius', b.radius + 'px');
    if (b.button_radius != null) root.style.setProperty('--nm-button-radius', b.button_radius + 'px');
    if (b.tile_gap != null) root.style.setProperty('--nm-gap', b.tile_gap + 'px');
    if (b.tile_aspect) root.style.setProperty('--nm-aspect', b.tile_aspect);
    if (b.background_image_url) root.style.setProperty('--nm-bg-image', 'url("' + String(b.background_image_url).replace(/"/g, '') + '")');
    root.classList.remove('nm-style-outline', 'nm-style-ghost');
    if (b.button_style && b.button_style !== 'filled') root.classList.add('nm-style-' + b.button_style);
  }

  class MeetingUI {
    constructor(root, opts) {
      this.root = typeof root === 'string' ? document.querySelector(root) : root;
      this.opts = Object.assign({ title: '' }, opts || {});
      this.b = this.opts.branding || {};
      this.features = Object.assign({}, DEFAULT_FEATURES, this.b.features || {});
      this.layout = this.b.default_layout || 'grid';
      this.client = new NM.Client({ base: this.opts.base, token: this.opts.token, room: this.opts.room,
        name: this.opts.name, media: this.opts.media, settings: this.opts.settings });
      this.tiles = new Map();
      this.unread = 0;
      this.chatTo = '';
      this._build();
      this._wire();
      if (this.features.prejoin && this.opts.prejoin !== false) this._prejoin(); else this.join();
    }

    t(key, vars) {
      let s = (this.b.strings && this.b.strings[key]) || STRINGS[key] || key;
      Object.keys(vars || {}).forEach((k) => { s = s.split('{' + k + '}').join(vars[k]); });
      return s;
    }

    can(p) { return this.client.can(p); }

    _build() {
      const r = this.root;
      r.innerHTML = '';
      r.classList.add('nm-root');
      applyBranding(r, this.b);
      this.pinnedBar = el('div', 'nm-pinned');
      this.grid = el('div', 'nm-grid');
      this.strip = el('div', 'nm-strip');
      const row = el('div', 'nm-stage-row');
      row.append(this.grid, this.strip);
      this.mainEl = el('div', 'nm-main');
      this.mainEl.append(this.pinnedBar, row);
      this.side = el('aside', 'nm-side');
      const tabs = ['chat', 'people', 'polls', 'qa', 'breakouts'].filter((x) => x === 'chat' || x === 'people' || this.features[x]);
      this.side.innerHTML = '<div class="nm-tabs">' + tabs.map((x, i) => '<button data-tab="' + x + '"' + (i ? '' : ' class="nm-active"') + '></button>').join('') + '</div>' +
        tabs.map((x, i) => '<div class="nm-panel' + (i ? '' : ' nm-active') + '" data-panel="' + x + '"></div>').join('') +
        '<form class="nm-chatform"><select class="nm-to" aria-label="Recipient"></select><div class="nm-row"><input maxlength="4000" aria-label="Chat message"><button class="nm-mini nm-primary" type="submit"></button></div></form>';
      tabs.forEach((x) => { this.side.querySelector('[data-tab="' + x + '"]').textContent = this.t(x); });
      this.side.querySelector('.nm-chatform input').placeholder = this.t('message_ph');
      this.side.querySelector('.nm-chatform button').textContent = this.t('send');
      const stage = el('div', 'nm-stage');
      stage.append(this.mainEl, this.side);
      this.bar = el('div', 'nm-bar');
      const brand = el('span', 'nm-brand');
      const logo = this.b.logo_dark_url || this.b.logo_url;
      brand.innerHTML = (logo ? '<img alt="" src="' + esc(logo) + '">' : '') + '<span>' + esc(this.opts.title || this.b.name || '') + '</span>';
      this.bar.append(brand);
      this.btn = {};
      const toolbar = (this.b.toolbar && this.b.toolbar.length ? this.b.toolbar : DEFAULT_TOOLBAR);
      const featureOf = { chat: 'chat', people: 'people', reactions: 'reactions', settings: 'settings', layout: 'layout_switch',
        whiteboard: 'whiteboard', captions: 'captions' };
      toolbar.forEach((name) => {
        if (featureOf[name] && !this.features[featureOf[name]]) return;
        const b = el('button', 'nm-btn' + (name === 'leave' ? ' nm-leave' : ''));
        b.dataset.act = name;
        b.title = this.t(name);
        b.setAttribute('aria-label', this.t(name));
        b.innerHTML = ICONS[name] + (name === 'screen' ? '<span class="nm-lbl">' + esc(this.t('screen')) + '</span>' : '') +
          (name === 'chat' || name === 'people' ? '<span class="nm-count"></span>' : '');
        this.bar.append(b);
        this.btn[name] = b;
      });
      this.toasts = el('div', 'nm-toast');
      this.capBar = el('div', 'nm-captions');
      this.recBadge = el('span', 'nm-rec', '● ' + esc(this.t('rec')));
      brand.prepend(this.recBadge);
      this.mainEl.append(this.capBar);
      r.append(stage, this.bar, this.toasts);
      this.chatList = this.side.querySelector('[data-panel="chat"]');
      this.peopleList = this.side.querySelector('[data-panel="people"]');
      this.pollsEl = this.side.querySelector('[data-panel="polls"]');
      this.qaEl = this.side.querySelector('[data-panel="qa"]');
      this.boEl = this.side.querySelector('[data-panel="breakouts"]');
      this.showCaptions = true;
      this._applyLayout();
    }

    cover(html, wide) {
      this.uncover();
      const box = el('div', 'nm-screen-cover');
      box.innerHTML = '<div class="nm-card' + (wide ? ' nm-wide' : '') + '">' + html + this._powered() + '</div>';
      this.root.append(box);
      this.coverEl = box;
      return box;
    }

    uncover() { if (this.coverEl) { this.coverEl.remove(); this.coverEl = null; } }

    _powered() {
      return this.b.show_powered_by === false ? '' : '<div class="nm-powered">' + esc(this.t('powered_by', { name: 'nodemeet' })) + '</div>';
    }

    _logoHtml() {
      const logo = this.b.logo_url || this.b.logo_dark_url;
      return logo ? '<img class="nm-logo" alt="' + esc(this.b.name || '') + '" src="' + esc(logo) + '">' : '';
    }

    toast(text, ms, action) {
      if (!this.features.notifications && !action) return;
      const t = el('div', null, esc(text));
      if (action) {
        const b = el('button', 'nm-mini nm-primary', esc(action.label));
        b.onclick = () => { action.run(); t.remove(); };
        t.append(b);
      }
      this.toasts.append(t);
      setTimeout(() => t.remove(), ms || 3500);
    }

    beep(freq) {
      if (!this.features.sounds) return;
      try {
        const Ctx = global.AudioContext || global.webkitAudioContext;
        this._ac = this._ac || new Ctx();
        const o = this._ac.createOscillator(), g = this._ac.createGain();
        o.frequency.value = freq || 660; g.gain.value = 0.04;
        o.connect(g); g.connect(this._ac.destination); o.start(); o.stop(this._ac.currentTime + 0.12);
      } catch (e) { /* audio blocked */ }
    }

    async _prejoin() {
      const c = this.client, claims = c.claims || {};
      const box = this.cover(this._logoHtml() + '<h2>' + esc(this.b.prejoin_title || this.t('ready')) + '</h2>' +
        '<p>' + esc(this.b.prejoin_subtitle || this.opts.title || '') + '</p>' +
        (this.features.device_preview ? '<div class="nm-preview"><video autoplay playsinline muted></video></div><div class="nm-meter"><i></i></div>' : '') +
        '<label class="nm-field"><span>' + esc(this.t('your_name')) + '</span><input class="nm-input" name="name" maxlength="80" value="' + esc(this.opts.name || claims.name || '') + '"></label>' +
        '<label><input type="checkbox" name="mic" checked> ' + esc(this.t('mic_on')) + '</label>' +
        '<label><input type="checkbox" name="cam" checked> ' + esc(this.t('cam_on')) + '</label>' +
        (this.features.settings ? '<button class="nm-btn" data-x="settings" type="button">' + ICONS.settings + ' ' + esc(this.t('settings')) + '</button>' : '') +
        '<button class="nm-btn nm-primary" data-x="join" type="button">' + esc(this.t('join_now')) + '</button><p class="nm-err"></p>');
      const q = (s) => box.querySelector(s);
      if (this.features.device_preview) this._startPreview(q('video'), q('.nm-meter i'));
      if (q('[data-x="settings"]')) q('[data-x="settings"]').onclick = () => this.openSettings(true);
      q('[data-x="join"]').onclick = () => {
        c.name = q('[name="name"]').value.trim() || null;
        c.audioMuted = !q('[name="mic"]').checked;
        c.videoMuted = !q('[name="cam"]').checked;
        q('[data-x="join"]').disabled = true;
        this._stopPreview();
        this.join().then(() => this.uncover(), (err) => {
          if (err && err.code === 'denied') return this._end(this.t('denied'), false);
          if (err && err.code === 'banned') return this._end(this.t('banned_rejoin'), false);
          q('[data-x="join"]').disabled = false;
          q('.nm-err').textContent = this.t('could_not_join', { error: err.message });
        });
      };
    }

    async _startPreview(video, meter) {
      try {
        const st = this.client.settings;
        this._preview = await navigator.mediaDevices.getUserMedia({
          video: st.cameraId ? { deviceId: { exact: st.cameraId } } : true,
          audio: st.microphoneId ? { deviceId: { exact: st.microphoneId } } : true });
        video.srcObject = this._preview;
        if (meter) this._meter(this._preview, meter);
      } catch (e) { /* no devices or denied; the join will report it */ }
    }

    _stopPreview() {
      if (this._preview) { this._preview.getTracks().forEach((t) => t.stop()); this._preview = null; }
      if (this._meterStop) { this._meterStop(); this._meterStop = null; }
    }

    _meter(stream, bar) {
      try {
        const Ctx = global.AudioContext || global.webkitAudioContext;
        const ctx = new Ctx(), an = ctx.createAnalyser();
        ctx.createMediaStreamSource(stream).connect(an);
        const data = new Uint8Array(an.frequencyBinCount);
        let on = true;
        const tick = () => {
          if (!on) return;
          an.getByteFrequencyData(data);
          bar.style.width = Math.min(100, (data.reduce((a, b) => a + b, 0) / data.length) * 2) + '%';
          requestAnimationFrame(tick);
        };
        tick();
        this._meterStop = () => { on = false; ctx.close(); };
      } catch (e) { /* no WebAudio */ }
    }

    join() { return this.client.join(); }

    _end(text, rejoin) {
      if (this.b.leave_redirect_url && rejoin !== false && text === this.t('left')) {
        global.location.href = this.b.leave_redirect_url;
        return;
      }
      const box = this.cover(this._logoHtml() + '<h2>' + esc(text) + '</h2>' +
        (this.b.end_message ? '<p>' + esc(this.b.end_message) + '</p>' : '') +
        (rejoin === false ? '' : '<button class="nm-btn nm-primary">' + esc(this.t('rejoin')) + '</button>'));
      const b = box.querySelector('button');
      if (b) b.onclick = () => { this.uncover(); this.client.join().catch((e) => this._end(e.message)); };
    }

    _wire() {
      const c = this.client;
      this.bar.addEventListener('click', (ev) => {
        const b = ev.target.closest('[data-act]');
        if (!b) return;
        if (MENU_ACTS[b.dataset.act]) {
          // a menu button: the root's "click outside closes the menu" handler must not see this click,
          // or it closes the menu the same instant it opens (bug before 0.9.3: More, Reactions and Layout did nothing)
          ev.stopPropagation();
          if (this.menuEl && this.menuAnchor === b) { this._closeMenu(); return; }   // second click closes it
        }
        this._action(b.dataset.act, b);
      });
      this._onKey = (ev) => { if (ev.key === 'Escape' && this.menuEl) { const a = this.menuAnchor; this._closeMenu(); if (a && a.focus) a.focus(); } };
      global.document.addEventListener('keydown', this._onKey);
      this.side.querySelectorAll('[data-tab]').forEach((tb) => tb.addEventListener('click', () => this._tab(tb.dataset.tab)));
      this.side.addEventListener('click', (ev) => this._sideAction(ev));
      this.side.addEventListener('submit', (ev) => this._sideSubmit(ev), true);
      this.side.addEventListener('change', (ev) => {
        const sel = ev.target.closest('[data-bo-move]');
        if (sel && sel.value) this.client.breakout('move', { user_id: sel.dataset.boMove, room: sel.value });
      });
      this.side.querySelector('.nm-chatform').addEventListener('submit', (ev) => {
        ev.preventDefault();
        const input = ev.target.querySelector('input');
        const to = ev.target.querySelector('.nm-to').value;
        c.sendChat(input.value, to || undefined);
        input.value = '';
      });
      this.root.addEventListener('click', (ev) => {
        const b = ev.target.closest('[data-peer-menu]');
        if (b) { ev.stopPropagation(); this._peerMenu(b.dataset.peerMenu, b); return; }
        const tool = ev.target.closest('[data-chat]');
        if (tool) { if (tool.dataset.chat === 'delete') c.deleteChat(tool.dataset.id); else c.pinChat(tool.dataset.chat === 'pin' ? tool.dataset.id : null); return; }
        const lob = ev.target.closest('[data-lobby]');
        if (lob) {
          const a = lob.dataset.lobby, id = lob.dataset.peer;
          if (a === 'all') c.admitAll();
          else if (a === 'admit') c.admit(id);
          else if (a === 'always') c.admitAlways(id);
          else if (a === 'block') { if (global.confirm(this.t('confirm_block', { name: lob.dataset.name || '' }))) c.block(id); }
          else if (a === 'unban') c.unban(id);
          else c.deny(id);
          return;
        }
        if (!ev.target.closest('.nm-menu')) this._closeMenu();
      });
      c.on('lobby', (m) => this.cover(this._logoHtml() + '<h2>' + esc(this.t('waiting_title')) + '</h2><p class="nm-lobby-msg">' +
        esc(m.message || this.b.lobby_message || this.t('waiting_text')) + '</p><p class="nm-lobby-pos">' + (m.position ? esc(this.t('position', { n: m.position })) : '') + '</p>'));
      c.on('admitted', () => this.uncover());
      c.on('joined', () => {
        if (!this.opts.branding && c.branding) { this.b = c.branding; this.features = Object.assign({}, DEFAULT_FEATURES, this.b.features || {}); applyBranding(this.root, this.b); }
        this._tile(c.peerId, true); this._refresh(); this._renderChatTo();
        if (c.room && c.room.pinned) this._pinned(c.room.pinned);
        if (this.can('moderate.lobby') && global.Notification && Notification.permission === 'default') {
          try { Notification.requestPermission(); } catch (e) { /* ignore */ }
        }
      });
      c.on('local-stream', ({ stream }) => this._setVideo(c.peerId, stream));
      c.on('screen-share', () => this._setVideo(c.peerId, c.screenStream || c.localStream));
      c.on('participants', () => { this._refresh(); this._renderChatTo(); });
      c.on('participant-joined', (p) => { this._tile(p.peer_id, false); this.toast(this.t('joined', { name: p.name })); this.beep(880); });
      c.on('participant-left', ({ peerId }) => { const t = this.tiles.get(peerId); if (t) { t.el.remove(); this.tiles.delete(peerId); } this._applyLayout(); });
      c.on('stream', ({ peerId, stream }) => { this._tile(peerId, false); this._setVideo(peerId, stream); });
      c.on('stream-removed', ({ peerId }) => this._setVideo(peerId, null));
      c.on('chat', (m) => this._chat(m));
      c.on('chat-deleted', ({ id }) => { const n = this.chatList.querySelector('[data-mid="' + CSS.escape(id) + '"]'); if (n) n.remove(); });
      c.on('chat-pinned', (m) => this._pinned(m));
      c.on('reaction', (m) => this._float(m));
      c.on('spotlight', (m) => { this._applyLayout(); if (m.peer_id) this.toast(this.t('spotlighted', { name: this._info(m.peer_id).name })); });
      c.on('local-state', () => this._refresh());
      c.on('you-updated', () => this._refresh());
      c.on('settings', () => { this._refresh(); this.tiles.forEach((t) => c.applySpeaker(t.video)); });
      c.on('lobby-update', (list) => {
        this._refresh();
        const ids = new Set((list || []).map((w) => w.peer_id)), fresh = (list || []).filter((w) => !(this._lobbySeen || new Set()).has(w.peer_id));
        this._lobbySeen = ids;
        if (fresh.length) {  // knock knock
          this.beep(520); setTimeout(() => this.beep(700), 160);
          fresh.forEach((w) => this.toast(this.t('knock', { name: w.name }), 8000, { label: this.t('admit'), run: () => c.admit(w.peer_id) }));
          if (document.hidden && global.Notification && Notification.permission === 'granted') {
            try { new Notification(this.t('knock', { name: fresh[0].name })); } catch (e) { /* ignore */ }
          }
        }
      });
      c.on('topology', ({ topology }) => this.toast(this.t(topology === 'sfu' ? 'switched_sfu' : 'switched_p2p')));
      c.on('force-mute', (m) => this.toast(this.t('muted_by', { by: m.by })));
      c.on('force-video-off', (m) => this.toast(this.t('video_off_by', { by: m.by })));
      c.on('force-stop-screen', (m) => this.toast(this.t('screen_stopped', { by: m.by })));
      c.on('unmute-request', (m) => this.toast(this.t('unmute_req', { by: m.by }), 10000, { label: this.t('unmute_btn'), run: () => c.setAudioMuted(false) }));
      c.on('unmute-allowed', () => this.toast(this.t('allowed_unmute')));
      c.on('video-allowed', () => this.toast(this.t('allowed_video')));
      c.on('kicked', (m) => this._end(this.t('kicked', { by: m.by })));
      c.on('banned', () => this._end(this.t(c.peerId ? 'banned' : 'banned_rejoin'), false));
      c.on('ban-attempt', (m) => this.toast(this.t('ban_attempt', { name: m.name || m.user_id }), 12000,
        { label: this.t('unban'), run: () => c.unban(m.user_id) }));
      c.on('denied', () => this._end(this.t('denied'), false));
      c.on('room-closed', (m) => this._end(m.reason === 'ended' ? this.t('ended') : this.t('ended'), false));
      c.on('room-updated', () => this._refresh());
      c.on('left', (m) => { if (m.reason !== 'left' && m.reason !== 'switching') this._end(this.t('disconnected')); });
      c.on('switching', ({ room }) => { this.tiles.forEach((t) => t.el.remove()); this.tiles.clear(); this.chatList.innerHTML = ''; this.toast(this.t('moving', { name: room })); });
      c.on('features', () => this._renderFeatures());
      ['poll', 'poll-removed'].forEach((e) => c.on(e, () => { this._renderPolls(); if (e === 'poll') this._badge('polls'); }));
      ['question', 'question-removed'].forEach((e) => c.on(e, () => { this._renderQA(); if (e === 'question') this._badge('qa'); }));
      c.on('board', () => this._drawBoard());
      c.on('breakouts', () => this._renderBreakouts());
      c.on('breakout-assign', (m) => this._assigned(m));
      c.on('breakout-closing', (m) => this.toast(this.t('closing_in', { n: m.seconds }), 6000));
      c.on('announcement', (m) => this.toast(this.t('announcement', { by: m.by, text: m.text }), 9000));
      c.on('recording', (r) => { this._refresh(); this.toast(r ? this.t('rec_started', { by: r.by }) : this.t('rec_stopped'), 5000); });
      c.on('recording-saved', () => this.toast(this.t('rec_saved')));
      c.on('caption', (m) => this._caption(m));
      c.on('captions-state', () => this._refresh());
      c.on('lobby-message', (m) => { const p = this.coverEl && this.coverEl.querySelector('.nm-lobby-msg'); if (p) p.textContent = m.text || this.t('waiting_text'); });
      c.on('lobby-position', (m) => { const p = this.coverEl && this.coverEl.querySelector('.nm-lobby-pos'); if (p) p.textContent = this.t('position', { n: m.position }); });
      c.on('disconnected', () => { this.tiles.forEach((t) => t.el.remove()); this.tiles.clear(); this.chatList.innerHTML = ''; });
      c.on('reconnecting', ({ attempt }) => this.toast(this.t('reconnecting', { n: attempt }), 2000));
      c.on('media-error', (m) => this.toast(this.t('media_error', { error: m.message }), 6000));
      c.on('permissions', (m) => { this.toast(this.t(m.can_publish ? 'can_speak' : 'watch_only')); if (m.you) this.toast(this.t('role_changed', { role: m.you.role_label || m.you.role })); this._refresh(); });
      c.on('error', (e) => { if (e && e.message) this.toast(e.message); });
      global.addEventListener('resize', () => { this._applyLayout(); this._drawBoard(); });
    }

    _action(act, anchor) {
      const c = this.client;
      if (act === 'mic') c.toggleAudio();
      else if (act === 'camera') c.toggleVideo();
      else if (act === 'screen') (c.screenStream ? c.stopScreenShare() : c.startScreenShare()).catch((e) => this.toast(e.message));
      else if (act === 'hand') c.toggleHand();
      else if (act === 'chat' || act === 'people') this._toggleSide(act);
      else if (act === 'settings') this.openSettings(false);
      else if (act === 'reactions') this._reactionMenu(anchor);
      else if (act === 'layout') this._layoutMenu(anchor);
      else if (act === 'more') this._moreMenu(anchor);
      else if (act === 'whiteboard') this._toggleBoard();
      else if (act === 'captions') { this.showCaptions = !this.showCaptions; this._renderCaptions(); this._refresh(); }
      else if (act === 'leave') {
        c.leave();
        if (this.opts.onLeave) this.opts.onLeave(); else this._end(this.t('left'));
      }
    }

    _closeMenu() {
      if (this.menuEl) { this.menuEl.remove(); this.menuEl = null; }
      if (this.menuAnchor) { this.menuAnchor.setAttribute('aria-expanded', 'false'); this.menuAnchor = null; }
    }

    _menu(anchor, items) {
      this._closeMenu();
      const m = el('div', 'nm-menu');
      items.filter(Boolean).forEach((it) => {
        if (it.html) { m.append(el('div', null, it.html)); return; }
        const b = el('button', it.danger ? 'nm-danger' : '', esc(it.label));
        b.onclick = (ev) => { ev.stopPropagation(); this._closeMenu(); it.run(); };
        m.append(b);
      });
      if (!m.childNodes.length) return;
      this.root.append(m);
      const r = anchor.getBoundingClientRect(), base = this.root.getBoundingClientRect();
      const left = Math.min(Math.max(8, r.left - base.left), base.width - m.offsetWidth - 8);
      const top = r.top - base.top - m.offsetHeight - 8;
      m.style.left = left + 'px';
      m.style.top = (top > 8 ? top : r.bottom - base.top + 8) + 'px';
      m.setAttribute('role', 'menu');
      this.menuEl = m;
      this.menuAnchor = anchor;
      anchor.setAttribute('aria-expanded', 'true');
      const first = m.querySelector('button');
      if (first) first.focus({ preventScroll: true });
    }

    _reactionMenu(anchor) {
      if (!this.can('reactions.send')) return;
      const list = this.b.reactions || ['👍', '👏', '😂', '❤️', '🎉', '😮'];
      this._menu(anchor, [{ html: '<div class="nm-reactions">' + list.map((e) => '<button data-emoji="' + esc(e) + '">' + esc(e) + '</button>').join('') + '</div>' }]);
      this.menuEl.querySelectorAll('[data-emoji]').forEach((b) => { b.onclick = () => { this.client.react(b.dataset.emoji); this._closeMenu(); }; });
    }

    _layoutMenu(anchor) {
      if (!this.can('layout.change')) return;
      this._menu(anchor, ['grid', 'speaker', 'sidebar'].map((l) => ({ label: (this.layout === l ? '✓ ' : '') + this.t(l), run: () => { this.layout = l; this._preSpot = null; this._applyLayout(); } })));
    }

    _moreMenu(anchor) { this._menu(anchor, this._moreItems()); }

    _moreItems() {
      const c = this.client, room = c.room || {};
      const confirmIt = (msg, fn) => () => { if (global.confirm(msg)) fn(); };
      return [
        this.can('moderate.mute_all') && { label: this.t('mute_all'), run: () => c.muteAll() },
        this.can('moderate.lower_hands') && { label: this.t('lower_all'), run: () => c.lowerAllHands() },
        this.can('moderate.lock') && { label: this.t(room.locked ? 'unlock' : 'lock'), run: () => c.lockRoom(!room.locked) },
        this.can('moderate.lobby') && { label: this.t(room.lobby ? 'lobby_off' : 'lobby_on'), run: () => c.setLobby(!room.lobby) },
        this.can('moderate.chat') && { label: this.t(room.chat_enabled === false ? 'chat_enable' : 'chat_disable'), run: () => c.setChatEnabled(room.chat_enabled === false) },
        this.features.recording && this.can('recording.start') && { label: this.t(c.recording ? 'rec_stop' : 'rec_start'), run: () => { try { c.recording ? c.stopRecording() : c.startRecording(); } catch (e) { this.toast(e.message); } } },
        this.features.captions && this.can('captions.enable') && { label: this.t(c.captions && c.captions.enabled ? 'cc_off' : 'cc_on'), run: () => c.enableCaptions(!(c.captions && c.captions.enabled)) },
        this.can('transcript.download') && { label: this.t('transcript'), run: () => global.open((this.opts.base || '') + '/api/rooms/' + encodeURIComponent(c.roomId || room.id) + '/transcript?format=txt&token=' + encodeURIComponent(c.token)) },
        this.can('moderate.lobby') && { label: this.t('lobby_msg'), run: () => { const t = global.prompt(this.t('lobby_msg'), room.lobby_message || ''); if (t !== null) c.setLobbyMessage(t); } },
        this.can('moderate.lobby') && { label: this.t(room.lobby_mode === 'until_host' ? 'lobby_manual' : 'lobby_until_host'), run: () => c.setLobbyMode(room.lobby_mode === 'until_host' ? 'manual' : 'until_host') },
        this.can('profile.rename') && { label: this.t('rename_self'), run: () => { const n = global.prompt(this.t('rename_self'), (c.you && c.you.name) || ''); if (n) c.rename(n); } },
        this.features.fullscreen && { label: this.t('fullscreen'), run: () => (document.fullscreenElement ? document.exitFullscreen() : this.root.requestFullscreen()).catch(() => {}) },
        this.features.picture_in_picture && document.pictureInPictureEnabled && { label: this.t('pip'), run: () => this._pip() },
        this.can('moderate.end') && { label: this.t('end_meeting'), danger: true, run: confirmIt(this.t('confirm_end'), () => c.endMeeting()) },
      ].filter(Boolean);
    }

    _pip() {
      const spot = this.client.room && this.client.room.spotlight;
      const t = (spot && this.tiles.get(spot)) || Array.from(this.tiles.values()).find((x) => !x.self) || this.tiles.get(this.client.peerId);
      if (t && t.video.requestPictureInPicture) t.video.requestPictureInPicture().catch(() => {});
    }

    _peerMenu(peerId, anchor) {
      const c = this.client, p = this._info(peerId), me = c.you || {};
      if (!p || p.self) return;
      const outranked = !this.can('moderate.override_rank') && (p.rank || 0) >= (me.rank || 0);
      const mod = (perm) => this.can(perm) && !outranked && !(p.attributes && p.attributes.immune);
      const confirmIt = (msg, fn) => () => { if (global.confirm(msg)) fn(); };
      const roles = (c.roles || []).filter((r) => r.name !== p.role && (this.can('moderate.override_rank') || r.rank < (me.rank || 0)));
      const items = [
        this.features.private_chat && this.can('chat.private') && { label: this.t('message_private'), run: () => { this.chatTo = peerId; this._renderChatTo(); this._toggleSide('chat', true); } },
        mod('moderate.mute') && !p.audio_muted && { label: this.t('mute'), run: () => c.mute(peerId) },
        mod('moderate.ask_unmute') && p.audio_muted && { label: this.t(p.audio_locked ? 'allow_unmute' : 'ask_unmute'), run: () => (p.audio_locked ? c.allowUnmute(peerId) : c.askUnmute(peerId)) },
        mod('moderate.video_off') && (!p.video_muted ? { label: this.t('video_off'), run: () => c.videoOff(peerId) } : p.video_locked && { label: this.t('allow_video'), run: () => c.allowVideo(peerId) }),
        mod('moderate.stop_screen') && p.screen_sharing && { label: this.t('stop_screen'), run: () => c.stopScreen(peerId) },
        mod('moderate.lower_hands') && p.hand_raised && { label: this.t('lower_hand'), run: () => c.lowerHand(peerId) },
        this.can('moderate.spotlight') && { label: this.t(c.room && c.room.spotlight === peerId ? 'unspotlight' : 'spotlight'), run: () => c.spotlight(c.room && c.room.spotlight === peerId ? null : peerId) },
        mod('moderate.rename') && { label: this.t('rename'), run: () => { const n = global.prompt(this.t('rename'), p.name); if (n) c.renameParticipant(peerId, n); } },
        mod('moderate.stage') && c.room && c.room.mode === 'webinar' && { label: this.t(p.can_publish ? 'remove_stage' : 'invite_stage'), run: () => (p.can_publish ? c.demote(peerId) : c.promote(peerId)) },
      ];
      if (mod('moderate.assign_roles') && roles.length) {
        roles.forEach((r) => items.push({ label: this.t('set_role') + ': ' + r.label, run: () => c.setRole(peerId, r.name) }));
      }
      const listed = ((c.room && c.room.join_list) || []).some((u) => String(u).toLowerCase() === String(p.user_id || '').toLowerCase());
      if (mod('moderate.lobby') && p.user_id) items.push({ label: this.t(listed ? 'remove_join_list' : 'add_join_list'), run: () => (listed ? c.removeFromJoinList(peerId) : c.addToJoinList(peerId)) });
      if (mod('moderate.kick')) items.push({ label: this.t('kick'), danger: true, run: confirmIt(this.t('confirm_kick', { name: p.name }), () => c.kick(peerId)) });
      if (mod('moderate.ban')) items.push({ label: this.t('ban'), danger: true, run: confirmIt(this.t('confirm_ban', { name: p.name }), () => c.ban(peerId)) });
      if (mod('moderate.ban')) items.push({ label: this.t('ban_device'), danger: true, run: confirmIt(this.t('confirm_ban', { name: p.name }), () => c.ban(peerId, ['user', 'device', 'ip'])) });
      this._menu(anchor, items);
    }

    async openSettings(fromPrejoin) {
      const c = this.client, st = c.settings;
      const dev = await c.listDevices();
      const opt = (list, cur) => '<option value="">' + esc(this.t('default_dev')) + '</option>' +
        list.map((d) => '<option value="' + esc(d.id) + '"' + (d.id === cur ? ' selected' : '') + '>' + esc(d.label) + '</option>').join('');
      const q = ['auto', 'low', 'medium', 'high', 'hd'].filter((k) => k !== 'hd' || c.can('media.hd') || !c.you);
      const chk = (k, label) => '<label><input type="checkbox" data-s="' + k + '"' + (st[k] ? ' checked' : '') + '> ' + esc(this.t(label)) + '</label>';
      const prev = this.coverEl && fromPrejoin ? this.coverEl : null;
      if (prev) prev.style.display = 'none';
      const box = el('div', 'nm-screen-cover');
      box.innerHTML = '<div class="nm-card nm-wide"><h2>' + esc(this.t('settings_title')) + '</h2>' +
        '<label class="nm-field"><span>' + esc(this.t('cam_label')) + '</span><select data-s="cameraId">' + opt(dev.cameras, st.cameraId) + '</select></label>' +
        '<label class="nm-field"><span>' + esc(this.t('mic_label')) + '</span><select data-s="microphoneId">' + opt(dev.microphones, st.microphoneId) + '</select></label>' +
        '<div class="nm-meter"><i></i></div>' +
        (dev.speakerSelection ? '<label class="nm-field"><span>' + esc(this.t('speaker_label')) + '</span><select data-s="speakerId">' + opt(dev.speakers, st.speakerId) + '</select></label>' : '') +
        '<button class="nm-btn nm-btn-sm" type="button" data-x="test">' + esc(this.t('test_speaker')) + '</button>' +
        '<label class="nm-field"><span>' + esc(this.t('quality')) + '</span><select data-s="quality">' +
        q.map((k) => '<option value="' + k + '"' + (st.quality === k ? ' selected' : '') + '>' + esc(this.t('q_' + k)) + '</option>').join('') + '</select></label>' +
        '<div class="nm-checks">' + chk('echoCancellation', 'echo') + chk('noiseSuppression', 'noise') + chk('autoGainControl', 'agc') +
        (dev.blurSupported ? chk('backgroundBlur', 'blur') : '') + chk('mirror', 'mirror') + (this.features.self_view ? chk('hideSelf', 'hide_self') : '') + '</div>' +
        '<div class="nm-card-actions"><button class="nm-btn" data-x="close" type="button">' + esc(this.t('close')) +
        '</button><button class="nm-btn nm-primary" data-x="save" type="button">' + esc(this.t('save')) + '</button></div></div>';
      this.root.append(box);
      let meterStream = null;
      const meter = box.querySelector('.nm-meter i');
      const startMeter = async (id) => {
        if (this._meterStop) this._meterStop();
        if (meterStream) meterStream.getTracks().forEach((t) => t.stop());
        try { meterStream = await navigator.mediaDevices.getUserMedia({ audio: id ? { deviceId: { exact: id } } : true }); this._meter(meterStream, meter); } catch (e) { /* ignore */ }
      };
      startMeter(st.microphoneId);
      box.querySelector('[data-s="microphoneId"]').onchange = (e) => startMeter(e.target.value);
      box.querySelector('[data-x="test"]').onclick = () => this._testSpeaker(box.querySelector('[data-s="speakerId"]'));
      const close = () => {
        if (this._meterStop) this._meterStop();
        if (meterStream) meterStream.getTracks().forEach((t) => t.stop());
        box.remove();
        if (prev) prev.style.display = '';
      };
      box.querySelector('[data-x="close"]').onclick = close;
      box.querySelector('[data-x="save"]').onclick = async () => {
        const patch = {};
        box.querySelectorAll('[data-s]').forEach((i) => { patch[i.dataset.s] = i.type === 'checkbox' ? i.checked : (i.value || null); });
        patch.quality = patch.quality || 'auto';
        close();
        await c.updateSettings(patch);
        this.tiles.forEach((t) => { if (t.self) t.el.style.display = c.settings.hideSelf ? 'none' : ''; });
        if (prev) { const v = prev.querySelector('.nm-preview video'); if (v) { this._stopPreview(); this._startPreview(v, prev.querySelector('.nm-meter i')); } }
      };
    }

    _testSpeaker(select) {
      // 0.5s 440 Hz tone as a WAV data URI, played through the chosen output device
      const rate = 8000, n = rate / 2, buf = new DataView(new ArrayBuffer(44 + n));
      const w = (o, s) => { for (let i = 0; i < s.length; i++) buf.setUint8(o + i, s.charCodeAt(i)); };
      w(0, 'RIFF'); buf.setUint32(4, 36 + n, true); w(8, 'WAVEfmt '); buf.setUint32(16, 16, true); buf.setUint16(20, 1, true);
      buf.setUint16(22, 1, true); buf.setUint32(24, rate, true); buf.setUint32(28, rate, true); buf.setUint16(32, 1, true);
      buf.setUint16(34, 8, true); w(36, 'data'); buf.setUint32(40, n, true);
      for (let i = 0; i < n; i++) buf.setUint8(44 + i, 128 + 60 * Math.sin(2 * Math.PI * 440 * i / rate));
      const audio = new Audio(URL.createObjectURL(new Blob([buf], { type: 'audio/wav' })));
      const id = select && select.value;
      (id && audio.setSinkId ? audio.setSinkId(id) : Promise.resolve()).then(() => audio.play()).catch(() => {});
    }

    _toggleSide(tab, forceOpen) {
      const open = this.root.classList.contains('nm-side-open');
      if (open && this.activeTab === tab && !forceOpen) this.root.classList.remove('nm-side-open');
      else { this.root.classList.add('nm-side-open'); this._tab(tab); }
      this._applyLayout();
    }

    _tab(tab) {
      this.activeTab = tab;
      this.side.querySelectorAll('[data-tab]').forEach((x) => x.classList.toggle('nm-active', x.dataset.tab === tab));
      this.side.querySelectorAll('[data-panel]').forEach((p) => p.classList.toggle('nm-active', p.dataset.panel === tab));
      this.side.querySelector('.nm-chatform').style.display = tab === 'chat' && this.can('chat.send') ? '' : 'none';
      const tb = this.side.querySelector('[data-tab="' + tab + '"]');
      if (tb) delete tb.dataset.dot;
      if (tab === 'chat') { this.unread = 0; this._refresh(); }
      if (tab === 'polls') this._renderPolls();
      if (tab === 'qa') this._renderQA();
      if (tab === 'breakouts') this._renderBreakouts();
    }

    _renderChatTo() {
      const sel = this.side.querySelector('.nm-to');
      const allowed = this.features.private_chat && this.can('chat.private');
      sel.style.display = allowed ? '' : 'none';
      if (!allowed) { this.chatTo = ''; return; }
      const people = this.client.participantList();
      sel.innerHTML = '<option value="">' + esc(this.t('everyone')) + '</option>' +
        people.map((p) => '<option value="' + esc(p.peer_id) + '">' + esc(this.t('private_to', { name: p.name })) + '</option>').join('');
      sel.value = people.some((p) => p.peer_id === this.chatTo) ? this.chatTo : '';
      sel.onchange = () => { this.chatTo = sel.value; };
    }

    // ---- polls, Q&A, breakouts, whiteboard, captions -------------------------------------
    _badge(tab) {
      const open = this.root.classList.contains('nm-side-open') && this.activeTab === tab;
      const t = this.side.querySelector('[data-tab="' + tab + '"]');
      if (t && !open) t.dataset.dot = '1';
    }

    _renderFeatures() { this._renderPolls(); this._renderQA(); this._renderBreakouts(); this._drawBoard(); this._refresh(); }

    _renderPolls() {
      const el_ = this.pollsEl, c = this.client;
      if (!el_) return;
      let html = '';
      if (this.can('polls.create')) {
        html += '<form class="nm-form" data-form="poll"><div class="nm-section">' + esc(this.t('new_poll')) + '</div>' +
          '<input class="nm-input" name="q" maxlength="300" placeholder="' + esc(this.t('question_ph')) + '">' +
          '<textarea class="nm-input" name="o" rows="3" placeholder="' + esc(this.t('options_ph')) + '"></textarea>' +
          '<label><input type="checkbox" name="m"> ' + esc(this.t('multiple')) + '</label>' +
          '<label><input type="checkbox" name="a" checked> ' + esc(this.t('anonymous')) + '</label>' +
          '<button class="nm-mini nm-primary">' + esc(this.t('create')) + '</button></form>';
      }
      Array.from(c.polls.values()).reverse().forEach((p) => {
        const mine = c.myVotes[p.id] || [], total = p.total_votes || 0, res = p.results;
        const sum = res ? Object.values(res).reduce((a, b) => a + b, 0) || 1 : 1;
        html += '<div class="nm-poll"><b>' + esc(p.question) + '</b> <small>' + esc(p.open ? this.t('votes', { n: total }) : this.t('closed')) + '</small>' +
          p.options.map((o) => {
            const pct = res ? Math.round(100 * (res[o.id] || 0) / sum) : null;
            const voteBtn = p.open && this.can('polls.vote') ? '<input type="' + (p.multiple ? 'checkbox' : 'radio') + '" name="v-' + esc(p.id) + '" value="' + esc(o.id) + '"' + (mine.indexOf(o.id) >= 0 ? ' checked' : '') + '> ' : '';
            return '<label class="nm-opt">' + voteBtn + esc(o.text) + (pct !== null ? ' <small>' + pct + '%</small><i style="width:' + pct + '%"></i>' : '') + '</label>';
          }).join('') +
          (p.open && this.can('polls.vote') ? '<button class="nm-mini nm-primary" data-poll="vote" data-id="' + esc(p.id) + '">' + esc(this.t('vote')) + '</button> ' : '') +
          (this.can('polls.create') ? (p.open ? '<button class="nm-mini" data-poll="close" data-id="' + esc(p.id) + '">' + esc(this.t('close_poll')) + '</button> ' : '') +
            '<button class="nm-mini" data-poll="delete" data-id="' + esc(p.id) + '">' + esc(this.t('delete')) + '</button>' : '') + '</div>';
      });
      el_.innerHTML = html;
    }

    _renderQA() {
      const el_ = this.qaEl, c = this.client;
      if (!el_) return;
      let html = '';
      if (this.can('qa.ask')) {
        html += '<form class="nm-form" data-form="qa"><textarea class="nm-input" name="t" rows="2" maxlength="1000" placeholder="' + esc(this.t('ask_ph')) + '"></textarea>' +
          '<label><input type="checkbox" name="a"> ' + esc(this.t('ask_anon')) + '</label><button class="nm-mini nm-primary">' + esc(this.t('ask')) + '</button></form>';
      }
      const qs = Array.from(c.questions.values()).sort((a, b) => (b.highlighted - a.highlighted) || (a.answered - b.answered) || (b.votes - a.votes) || (a.ts - b.ts));
      qs.forEach((q) => {
        const mod = this.can('qa.answer');
        html += '<div class="nm-q' + (q.highlighted ? ' nm-hl' : '') + '"><div class="nm-meta"><b>' + esc(q.by) + '</b>' + (q.answered ? '<em>' + esc(this.t('answered')) + '</em>' : '') + '</div>' +
          '<div class="nm-text"></div>' + (q.answer ? '<div class="nm-answer"><b>' + esc(q.answer.by) + ':</b> ' + esc(q.answer.text) + '</div>' : '') +
          '<div class="nm-row">' + (this.can('qa.upvote') ? '<button class="nm-mini" data-qa="up" data-id="' + esc(q.id) + '">▲ ' + q.votes + '</button>' : '<small>▲ ' + q.votes + '</small>') +
          (mod ? '<button class="nm-mini" data-qa="answer" data-id="' + esc(q.id) + '">' + esc(this.t('answer')) + '</button>' +
            '<button class="nm-mini" data-qa="done" data-id="' + esc(q.id) + '">' + esc(this.t('mark_answered')) + '</button>' +
            '<button class="nm-mini" data-qa="hl" data-id="' + esc(q.id) + '">' + esc(this.t('highlight')) + '</button>' +
            '<button class="nm-mini nm-danger" data-qa="dismiss" data-id="' + esc(q.id) + '">' + esc(this.t('dismiss')) + '</button>' : '') + '</div></div>';
      });
      el_.innerHTML = html;
      el_.querySelectorAll('.nm-q .nm-text').forEach((n, i) => { n.textContent = qs[i].text; });
    }

    _renderBreakouts() {
      const el_ = this.boEl, c = this.client, b = c.breakouts || {};
      if (!el_) return;
      let html = '';
      const names = {};
      c.participantList().forEach((p) => { names[p.user_id] = p.name; });
      if (this.can('breakout.manage')) {
        if (!b.status) {
          html += '<form class="nm-form" data-form="bo"><label class="nm-field"><span>' + esc(this.t('rooms_count')) + '</span><input class="nm-input" name="n" type="number" min="1" max="50" value="2"></label>' +
            '<label><input type="checkbox" name="auto" checked> ' + esc(this.t('auto_assign')) + '</label>' +
            '<label><input type="checkbox" name="choose"> ' + esc(this.t('let_choose')) + '</label>' +
            '<label class="nm-field"><span>' + esc(this.t('duration')) + '</span><input class="nm-input" name="d" type="number" min="1" max="600"></label>' +
            '<button class="nm-mini nm-primary">' + esc(this.t('create_rooms')) + '</button></form>';
        } else {
          const assign = b.assignments || {};
          const opts = (b.rooms || []).map((r) => '<option value="' + esc(r.id) + '">' + esc(r.name) + '</option>').join('');
          (b.rooms || []).forEach((r) => {
            html += '<div class="nm-section">' + esc(r.name) + ' (' + (assign[r.id] || []).length + ')' +
              (b.status === 'open' ? ' <button class="nm-mini" data-bo="hop" data-id="' + esc(r.id) + '">' + esc(this.t('join_room')) + '</button>' : '') + '</div>' +
              (assign[r.id] || []).map((u) => '<div class="nm-person"><span class="nm-pname">' + esc(names[u] || u) + '</span><select class="nm-mini" data-bo-move="' + esc(u) + '"><option value="">' + esc(this.t('move_to')) + '</option>' + opts + '<option value="' + esc(c.roomId || '') + '">' + esc(this.t('main_room')) + '</option></select></div>').join('');
          });
          html += '<div class="nm-row">' + (b.status === 'draft' ? '<button class="nm-mini nm-primary" data-bo="open">' + esc(this.t('open_rooms')) + '</button>' : '') +
            (b.status === 'open' ? '<button class="nm-mini nm-danger" data-bo="close">' + esc(this.t('close_rooms')) + '</button><button class="nm-mini" data-bo="msg">' + esc(this.t('broadcast')) + '</button>' : '') + '</div>';
        }
      } else if (b.status === 'open' && b.allow_choose && this.can('breakout.choose')) {
        (b.rooms || []).forEach((r) => { html += '<div class="nm-person"><span class="nm-pname">' + esc(r.name) + ' <small>(' + r.count + ')</small></span><button class="nm-mini nm-primary" data-bo="choose" data-id="' + esc(r.id) + '">' + esc(this.t('join_room')) + '</button></div>'; });
      }
      el_.innerHTML = html;
    }

    _assigned(m) {
      const go = () => { clearTimeout(this._goTimer); this.client.switchRoom(m.room, m.token).catch((e) => this.toast(e.message)); };
      this.toast(this.t('assigned', { name: m.name || m.room }), 10000, { label: this.t('go_now'), run: go });
      clearTimeout(this._goTimer);
      this._goTimer = setTimeout(go, 10000);
    }

    _sideAction(ev) {
      const c = this.client, x = ev.target.closest('[data-poll],[data-qa],[data-bo]');
      if (!x) return;
      const id = x.dataset.id;
      if (x.dataset.poll === 'vote') {
        const picked = Array.from(this.pollsEl.querySelectorAll('[name="v-' + CSS.escape(id) + '"]:checked')).map((i) => i.value);
        if (picked.length) c.vote(id, picked);
      } else if (x.dataset.poll === 'close') c.closePoll(id);
      else if (x.dataset.poll === 'delete') c.deletePoll(id);
      else if (x.dataset.qa === 'up') c.upvote(id);
      else if (x.dataset.qa === 'answer') { const t = global.prompt(this.t('answer_ph')); if (t) c.answer(id, { text: t, answered: true }); }
      else if (x.dataset.qa === 'done') c.answer(id, { answered: true });
      else if (x.dataset.qa === 'hl') c.answer(id, { highlighted: true });
      else if (x.dataset.qa === 'dismiss') c.answer(id, { dismiss: true });
      else if (x.dataset.bo === 'open') c.breakout('open');
      else if (x.dataset.bo === 'close') c.breakout('close', { seconds: 30 });
      else if (x.dataset.bo === 'msg') { const t = global.prompt(this.t('broadcast')); if (t) c.breakout('message', { text: t }); }
      else if (x.dataset.bo === 'hop') c.breakout('join', { room: id });
      else if (x.dataset.bo === 'choose') c.breakout('choose', { room: id });
    }

    _sideSubmit(ev) {
      const f = ev.target.closest('[data-form]');
      if (!f || f.classList.contains('nm-chatform')) return false;
      ev.preventDefault();
      const c = this.client, kind = f.dataset.form;
      if (kind === 'poll') {
        const opts = f.o.value.split('\n').map((s) => s.trim()).filter(Boolean);
        c.createPoll(f.q.value, opts, { multiple: f.m.checked, anonymous: f.a.checked });
      } else if (kind === 'qa') { c.ask(f.t.value, f.a.checked); f.t.value = ''; }
      else if (kind === 'bo') {
        c.breakout('set', { count: Number(f.n.value) || 2, assign: f.auto.checked ? 'auto' : 'manual', allow_choose: f.choose.checked,
          duration_minutes: f.d.value ? Number(f.d.value) : undefined });
      }
      return true;
    }

    _caption(m) {
      this._caps = (this._caps || []).filter((x) => !(x.peer_id === m.peer_id && !x.final));
      this._caps.push(m);
      this._caps = this._caps.slice(-3);
      this._renderCaptions();
      clearTimeout(this._capTimer);
      this._capTimer = setTimeout(() => { this._caps = []; this._renderCaptions(); }, 8000);
    }

    _renderCaptions() {
      if (!this.capBar) return;
      const on = this.showCaptions && (this._caps || []).length;
      this.capBar.style.display = on ? '' : 'none';
      this.capBar.innerHTML = on ? this._caps.map((x) => '<div><b>' + esc(x.name) + ':</b> ' + esc(x.text) + '</div>').join('') : '';
    }

    _toggleBoard() {
      if (this.boardEl) { this.boardEl.remove(); this.boardEl = null; this._refresh(); return; }
      const c = this.client, canDraw = this.can('whiteboard.draw');
      const box = el('div', 'nm-board');
      box.innerHTML = '<canvas></canvas><div class="nm-board-tools">' +
        (canDraw ? '<input type="color" value="#111111" data-b="color"><select data-b="width"><option>2</option><option selected>4</option><option>8</option><option>16</option></select>' +
          '<button class="nm-mini" data-b="pen">' + esc(this.t('pen')) + '</button><button class="nm-mini" data-b="eraser">' + esc(this.t('eraser')) + '</button>' +
          '<button class="nm-mini" data-b="undo">' + esc(this.t('undo')) + '</button>' : '') +
        (this.can('whiteboard.clear') ? '<button class="nm-mini nm-danger" data-b="clear">' + esc(this.t('clear')) + '</button>' : '') +
        '<button class="nm-mini" data-b="close">' + esc(this.t('close')) + '</button></div>';
      this.mainEl.append(box);
      this.boardEl = box;
      const cv = box.querySelector('canvas');
      let tool = 'pen', cur = null;
      box.addEventListener('click', (ev) => {
        const b = ev.target.closest('[data-b]');
        if (!b) return;
        const a = b.dataset.b;
        if (a === 'close') this._toggleBoard();
        else if (a === 'clear') c.clearBoard();
        else if (a === 'undo') c.undoStroke();
        else if (a === 'pen' || a === 'eraser') tool = a;
      });
      const pt = (ev) => { const r = cv.getBoundingClientRect(); return [Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width)), Math.max(0, Math.min(1, (ev.clientY - r.top) / r.height))]; };
      if (canDraw) {
        cv.addEventListener('pointerdown', (ev) => { cv.setPointerCapture(ev.pointerId); cur = { points: [pt(ev)], tool, color: box.querySelector('[data-b="color"]').value, width: Number(box.querySelector('[data-b="width"]').value) }; });
        cv.addEventListener('pointermove', (ev) => { if (!cur) return; cur.points.push(pt(ev)); this._drawBoard(cur); });
        const end = () => { if (cur && cur.points.length) c.draw(cur); cur = null; };
        cv.addEventListener('pointerup', end); cv.addEventListener('pointercancel', end);
      }
      this._drawBoard();
      this._refresh();
    }

    _drawBoard(live) {
      if (!this.boardEl) return;
      const cv = this.boardEl.querySelector('canvas'), r = cv.getBoundingClientRect();
      const dpr = global.devicePixelRatio || 1;
      if (cv.width !== Math.round(r.width * dpr)) { cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr); }
      const g = cv.getContext('2d');
      g.setTransform(1, 0, 0, 1, 0, 0);
      g.fillStyle = '#ffffff'; g.fillRect(0, 0, cv.width, cv.height);
      const strokes = (this.client.board || []).concat(live ? [live] : []);
      strokes.forEach((s) => {
        g.globalCompositeOperation = 'source-over';
        g.strokeStyle = s.tool === 'eraser' ? '#ffffff' : s.color; g.globalAlpha = s.tool === 'highlighter' ? 0.35 : 1;
        g.lineWidth = (s.tool === 'eraser' ? s.width * 4 : s.width) * dpr; g.lineCap = 'round'; g.lineJoin = 'round';
        g.beginPath();
        (s.points || []).forEach(([x, y], i) => (i ? g.lineTo(x * cv.width, y * cv.height) : g.moveTo(x * cv.width, y * cv.height)));
        g.stroke();
      });
      g.globalAlpha = 1;
    }

    _info(peerId) {
      const c = this.client;
      if (peerId === c.peerId) return Object.assign({ name: c.name || this.t('you') }, c.you || {}, { self: true, peer_id: peerId });
      return c.participants.get(peerId) || { peer_id: peerId, name: '?' };
    }

    _tile(peerId, self) {
      if (this.tiles.has(peerId)) return this.tiles.get(peerId);
      const p = this._info(peerId);
      if (p.hidden && !self) return null;
      const e = el('div', 'nm-tile nm-novideo' + (self ? ' nm-self' : ''));
      e.dataset.peer = peerId;
      e.innerHTML = '<video autoplay playsinline' + (self ? ' muted' : '') + '></video><div class="nm-avatar"><span></span></div>' +
        '<div class="nm-label"><span class="nm-tname"></span><span class="nm-state"></span></div><div class="nm-badge"></div>' +
        (self ? '' : '<button class="nm-mini nm-tile-menu" data-peer-menu="' + esc(peerId) + '" aria-label="More">⋯</button>');
      const t = { el: e, video: e.querySelector('video'), self: !!self };
      this.tiles.set(peerId, t);
      if (self && this.client.settings && this.client.settings.hideSelf) e.style.display = 'none';
      this.grid.append(e);
      this._refresh();
      this._applyLayout();
      return t;
    }

    _setVideo(peerId, stream) {
      const t = this.tiles.get(peerId) || this._tile(peerId, peerId === this.client.peerId);
      if (!t) return;
      t.video.srcObject = stream || null;
      if (!t.self) this.client.applySpeaker(t.video);
      const st = this.client.settings || {};
      t.el.classList.toggle('nm-mirror', !!(t.self && st.mirror !== false));
      t.el.classList.toggle('nm-screen', !!(t.self && this.client.screenStream));
      const hasVideo = !!(stream && stream.getVideoTracks().some((x) => x.readyState === 'live'));
      t.el.classList.toggle('nm-novideo', !hasVideo);
      if (stream && !t.self && stream.getAudioTracks().length) this._speaking(t, stream);
    }

    _speaking(t, stream) {
      try {
        const Ctx = global.AudioContext || global.webkitAudioContext;
        this._sac = this._sac || new Ctx();
        const an = this._sac.createAnalyser();
        this._sac.createMediaStreamSource(stream).connect(an);
        const data = new Uint8Array(an.frequencyBinCount);
        const tick = () => {
          if (!t.el.isConnected) return;
          an.getByteFrequencyData(data);
          t.el.classList.toggle('nm-speaking', data.reduce((a, b) => a + b, 0) / data.length > 12);
          setTimeout(tick, 250);
        };
        tick();
      } catch (e) { /* no WebAudio */ }
    }

    _applyLayout() {
      if (!this.grid) return;
      const spot = (this.client && this.client.room && this.client.room.spotlight) || null;
      // A new spotlight switches grid users to speaker view once; they can still pick Grid (it always works now),
      // and when the spotlight ends they go back to the layout they had. (Before 0.9.5 a spotlight silently
      // overrode Grid, so picking it did nothing.)
      if (spot !== this._lastSpot) {
        if (spot && !this._lastSpot && this.layout === 'grid') { this._preSpot = 'grid'; this.layout = 'speaker'; }
        if (!spot && this._preSpot) { this.layout = this._preSpot; this._preSpot = null; }
        this._lastSpot = spot;
      }
      const layout = this.layout;
      this.root.classList.remove('nm-layout-grid', 'nm-layout-speaker', 'nm-layout-sidebar');
      this.root.classList.add('nm-layout-' + layout);
      const all = Array.from(this.tiles.entries());
      all.forEach(([id, t]) => t.el.classList.toggle('nm-spot', id === spot));
      if (layout === 'grid') {
        all.forEach(([, t]) => this.grid.append(t.el));
        this.strip.style.display = 'none';
      } else {
        const mainId = (spot && this.tiles.has(spot) && spot) || (all.find(([, t]) => !t.self) || all[0] || [])[0];
        this.strip.style.display = '';
        all.forEach(([id, t]) => (id === mainId ? this.grid : this.strip).append(t.el));
      }
      this._fitGrid();
      if (!this._ro && global.ResizeObserver) {          // panels opening, window resizing, full screen: refit
        this._ro = new global.ResizeObserver(() => this._fitGrid());
        this._ro.observe(this.grid);
      }
    }

    /** Size the tiles in the main area so they all fit: the column count that gives the biggest tiles wins. */
    _fitGrid() {
      const g = this.grid;
      if (!g) return;
      const tiles = Array.from(g.children).filter((e) => e.classList.contains('nm-tile') && e.style.display !== 'none');
      const n = tiles.length || 1;
      const cs = global.getComputedStyle(g);
      const gap = parseFloat(cs.columnGap) || parseFloat(cs.gap) || 8;
      const W = g.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
      const H = g.clientHeight - (parseFloat(cs.paddingTop) || 0) - (parseFloat(cs.paddingBottom) || 0);
      if (W <= 0 || H <= 0) return;
      const ar = String(cs.getPropertyValue('--nm-aspect') || '16/9').split('/').map(Number);
      const aspect = ar.length === 2 && ar[0] > 0 && ar[1] > 0 ? ar[0] / ar[1] : 16 / 9;
      let best = { w: 0, cols: 1 };
      for (let cols = 1; cols <= n; cols++) {
        const rows = Math.ceil(n / cols);
        const w = Math.min((W - gap * (cols - 1)) / cols, ((H - gap * (rows - 1)) / rows) * aspect);
        if (w > best.w) best = { w, cols };
      }
      const w = Math.max(80, Math.floor(best.w));
      const key = best.cols + ':' + w;
      if (this._fitKey === key) return;                   // nothing changed: don't touch the DOM (no resize loop)
      this._fitKey = key;
      g.style.setProperty('--nm-tile-w', w + 'px');     // flex-wrap: a part-filled last row is centred
    }

    _refresh() {
      const c = this.client, me = c.you || {}, room = c.room || {};
      this.tiles.forEach((t, id) => {
        const p = this._info(id);
        const name = this.features.show_names ? p.name + (t.self ? ' (' + this.t('you') + ')' : '') : '';
        t.el.querySelector('.nm-tname').textContent = name;
        t.el.querySelector('.nm-avatar span').textContent = initials(p.name);
        t.el.querySelector('.nm-state').textContent = (p.audio_muted ? ' 🔇' : '') + (p.screen_sharing ? ' 🖥' : '');
        const badge = t.el.querySelector('.nm-badge');
        badge.textContent = (p.hand_raised ? '✋' : '') + (this.features.show_role_badges && p.badge ? ' ' + p.badge : '');
        if (p.color) t.el.style.setProperty('--nm-role-color', p.color);
        if (!t.self && (p.video_muted || !(t.video.srcObject && t.video.srcObject.getVideoTracks().length))) t.el.classList.add('nm-novideo');
        else if (!t.self) t.el.classList.remove('nm-novideo');
        if (t.self) t.el.classList.toggle('nm-novideo', !!c.videoMuted || !c.localStream);
      });
      const b = this.btn;
      const show = (name, on) => { if (b[name]) b[name].style.display = on ? '' : 'none'; };
      show('mic', c.canPublishKind('audio'));
      show('camera', c.canPublishKind('video'));
      show('screen', c.canPublishKind('screen'));
      show('hand', this.can('hand.raise'));
      show('reactions', this.can('reactions.send'));
      show('layout', this.can('layout.change'));
      show('more', this._moreItems().length > 0);          // nothing to offer this person: no dead button
      if (b.mic) { b.mic.innerHTML = c.audioMuted ? ICONS.micOff : ICONS.mic; b.mic.classList.toggle('nm-off', !!c.audioMuted); b.mic.classList.toggle('nm-locked', !!me.audio_locked); }
      if (b.camera) { b.camera.innerHTML = c.videoMuted ? ICONS.cameraOff : ICONS.camera; b.camera.classList.toggle('nm-off', !!c.videoMuted); b.camera.classList.toggle('nm-locked', !!me.video_locked); }
      if (b.screen) b.screen.classList.toggle('nm-on', !!c.screenStream);
      if (b.hand) b.hand.classList.toggle('nm-on', !!me.hand_raised);
      if (this.recBadge) this.recBadge.style.display = c.recording ? '' : 'none';
      show('captions', !!(c.captions && c.captions.enabled) && this.can('captions.view'));
      if (b.captions) b.captions.classList.toggle('nm-on', this.showCaptions);
      show('whiteboard', this.can('whiteboard.view'));
      if (b.whiteboard) b.whiteboard.classList.toggle('nm-on', !!this.boardEl);
      const boTab = this.side && this.side.querySelector('[data-tab="breakouts"]');
      if (boTab) boTab.style.display = this.can('breakout.manage') || (c.breakouts && c.breakouts.allow_choose && c.breakouts.status === 'open') ? '' : 'none';
      const people = c.participantList().filter((p) => !p.hidden);
      const lobby = c.lobby || [];
      const cnt = (name, n) => { const x = b[name] && b[name].querySelector('.nm-count'); if (x) x.textContent = n ? String(n) : ''; };
      cnt('people', people.length + 1 + (lobby.length ? ' +' + lobby.length : ''));
      cnt('chat', this.unread || '');
      // people panel
      const row = (p, self) => '<div class="nm-person"><span class="nm-pname">' + (p.badge ? esc(p.badge) + ' ' : '') + esc(p.name) +
        (self ? ' (' + esc(this.t('you')) + ')' : '') + ' <small>' + esc(p.role_label || p.role || '') + '</small></span><span>' +
        (p.hand_raised ? '✋' : '') + (p.audio_muted ? '🔇' : '🎤') + (p.video_muted ? '🚫' : '📷') + '</span>' +
        (self ? '' : '<button class="nm-mini" data-peer-menu="' + esc(p.peer_id) + '">⋯</button>') + '</div>';
      let html = '';
      if (lobby.length && this.can('moderate.lobby')) {
        html += '<div class="nm-section">' + esc(this.t('waiting_room')) + ' (' + lobby.length + ')</div>' +
          '<button class="nm-mini nm-primary" data-lobby="all">' + esc(this.t('admit_all')) + '</button>' +
          lobby.map((w) => '<div class="nm-person"><span class="nm-pname">' + esc(w.name) + '</span>' +
            '<button class="nm-mini nm-primary" data-lobby="admit" data-peer="' + esc(w.peer_id) + '">' + esc(this.t('admit')) + '</button>' +
            '<button class="nm-mini" data-lobby="always" data-peer="' + esc(w.peer_id) + '">' + esc(this.t('admit_always')) + '</button>' +
            '<button class="nm-mini" data-lobby="deny" data-peer="' + esc(w.peer_id) + '">' + esc(this.t('deny')) + '</button>' +
            (this.can('moderate.ban') ? '<button class="nm-mini nm-danger" data-lobby="block" data-name="' + esc(w.name) + '" data-peer="' + esc(w.peer_id) + '">' + esc(this.t('block')) + '</button>' : '') +
            '</div>').join('');
      }
      const banned = (room.banned || []);
      if (banned.length && this.can('moderate.ban')) {
        html += '<div class="nm-section">' + esc(this.t('blocked')) + ' (' + banned.length + ')</div>' +
          banned.map((x) => '<div class="nm-person"><span class="nm-pname">' + esc(x.name || x.user_id) + ' <small>' + esc(x.by ? '· ' + x.by : '') + '</small></span>' +
            '<button class="nm-mini" data-lobby="unban" data-peer="' + esc(x.user_id) + '">' + esc(this.t('unban')) + '</button></div>').join('');
      }
      html += '<div class="nm-section">' + esc(this.t('in_meeting')) + ' (' + (people.length + 1) + ')</div>' + row(this._info(c.peerId), true) +
        people.slice().sort((a, x) => (x.rank || 0) - (a.rank || 0)).map((p) => row(p, false)).join('');
      if (this.peopleList) this.peopleList.innerHTML = html;
      if (this.chatList) {
        let off = this.chatList.querySelector('.nm-chat-off');
        if (room.chat_enabled === false && !off) { off = el('div', 'nm-sub nm-chat-off', esc(this.t('chat_off'))); this.chatList.append(off); }
        if (room.chat_enabled !== false && off) off.remove();
      }
      const form = this.side && this.side.querySelector('.nm-chatform');
      if (form && this.activeTab === 'chat') form.style.display = this.can('chat.send') && room.chat_enabled !== false ? '' : 'none';
    }

    _chat(m) {
      if (!m || !this.chatList) return;
      if (!this.can('chat.read') && this.client.permissions && this.client.permissions.length) return;
      const mine = m.peer_id === this.client.peerId;
      const who = m.private ? (mine ? this.t('private_to', { name: this._info(m.to).name }) : this.t('private')) : '';
      const time = m.ts ? new Date(m.ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
      const tools = (this.can('chat.pin') ? '<button class="nm-mini" data-chat="pin" data-id="' + esc(m.id) + '">' + esc(this.t('pin')) + '</button>' : '') +
        ((this.can('chat.delete_any') || (mine && this.can('chat.delete_own'))) ? '<button class="nm-mini" data-chat="delete" data-id="' + esc(m.id) + '">' + esc(this.t('delete')) + '</button>' : '');
      const row = el('div', 'nm-msg' + (m.private ? ' nm-private' : ''));
      row.dataset.mid = m.id || '';
      row.innerHTML = '<div class="nm-meta"><b>' + esc(m.name || m.sender_name || '?') + '</b><span>' + esc(time) + '</span>' +
        (who ? '<em>' + esc(who) + '</em>' : '') + '<span class="nm-tools">' + tools + '</span></div><div class="nm-text"></div>';
      row.querySelector('.nm-text').textContent = m.text || '';
      this.chatList.append(row);
      this.chatList.scrollTop = this.chatList.scrollHeight;
      const open = this.root.classList.contains('nm-side-open') && this.activeTab === 'chat';
      if (!open && !m.history && !mine) { this.unread++; this._refresh(); this.beep(740); }
    }

    _pinned(m) {
      if (!this.pinnedBar) return;
      if (!m) { this.pinnedBar.innerHTML = ''; this.pinnedBar.style.display = 'none'; return; }
      this.pinnedBar.style.display = '';
      this.pinnedBar.innerHTML = '📌 <b>' + esc(this.t('pinned')) + ':</b> <span></span>' +
        (this.can('chat.pin') ? ' <button class="nm-mini" data-chat="unpin">' + esc(this.t('unpin')) + '</button>' : '');
      this.pinnedBar.querySelector('span').textContent = (m.name ? m.name + ': ' : '') + (m.text || '');
    }

    _float(m) {
      if (!this.features.reactions || !m) return;
      const t = this.tiles.get(m.peer_id || m.from) || this.tiles.get(this.client.peerId);
      if (!t) return;
      const f = el('div', 'nm-float');
      f.textContent = m.emoji || '';
      f.style.left = (30 + Math.random() * 40) + '%';
      t.el.append(f);
      setTimeout(() => f.remove(), 2500);
    }

    destroy() {
      try { this.client.leave(); } catch (e) { /* ignore */ }
      this._stopPreview();
      if (this._ac) this._ac.close().catch(() => {});
      if (this._sac) this._sac.close().catch(() => {});
      this.tiles.clear();
      this.root.innerHTML = '';
      this.root.classList.remove('nm-root');
    }
  }

  function mount(root, opts) { return new MeetingUI(root, opts); }

  NM.UI = { MeetingUI, mount, applyBranding, STRINGS, ICONS };
})(typeof window !== 'undefined' ? window : this);
