/*! nodemeet.js - vanilla JS client for nodemeet (PolyForm Noncommercial 1.0.0) */
(function (global) {
  'use strict';

  class Emitter {
    constructor() { this._h = {}; }
    on(ev, fn) { (this._h[ev] = this._h[ev] || []).push(fn); return () => this.off(ev, fn); }
    off(ev, fn) { this._h[ev] = (this._h[ev] || []).filter((f) => f !== fn); }
    once(ev, fn) { const off = this.on(ev, (d) => { off(); fn(d); }); return off; }
    emit(ev, data) {
      (this._h[ev] || []).slice().forEach((fn) => {
        try { fn(data); } catch (e) { console.error('[nodemeet] handler for', ev, 'failed', e); }
      });
      if (ev !== '*') (this._h['*'] || []).forEach((fn) => fn({ type: ev, data }));
    }
  }

  // Where this script was served from, so `base` can be left out: <script src="https://you.com/meet/static/nodemeet.js">
  const SCRIPT_BASE = (function () {
    try {
      const src = global.document && global.document.currentScript && global.document.currentScript.src;
      return src ? src.replace(/\/static\/[^/]*$/, '') : '';
    } catch (e) { return ''; }
  })();

  // Token from the option, or from the page URL (#token=... or ?token=...)
  function tokenFromPage() {
    try {
      const h = new URLSearchParams((global.location.hash || '').replace(/^#/, ''));
      return h.get('token') || new URLSearchParams(global.location.search || '').get('token') || null;
    } catch (e) { return null; }
  }

  function wsUrlFrom(base) {
    const u = new URL((base || '') + '/ws', global.location.href);
    u.protocol = u.protocol === 'https:' ? 'wss:' : 'ws:';
    return u.toString();
  }

  function decodeToken(token) {
    try {
      const part = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      return JSON.parse(decodeURIComponent(escape(atob(part + '==='.slice((part.length + 3) % 4)))));
    } catch (e) { return null; }
  }

  function waitForIce(pc, timeout) {
    if (pc.iceGatheringState === 'complete') return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => { pc.removeEventListener('icegatheringstatechange', check); resolve(); };
      const check = () => { if (pc.iceGatheringState === 'complete') done(); };
      pc.addEventListener('icegatheringstatechange', check);
      setTimeout(done, timeout || 2500);
    });
  }

  const SETTINGS_KEY = 'nodemeet:settings';
  function deviceId() {  // stable per browser; lets hosts block a device, not just an account
    try {
      let id = localStorage.getItem('nodemeet:device');
      if (!id) { id = 'dev_' + Math.random().toString(36).slice(2) + Date.now().toString(36); localStorage.setItem('nodemeet:device', id); }
      return id;
    } catch (e) { return null; }
  }
  function loadSettings() { try { return JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}'); } catch (e) { return {}; } }
  function saveSettings(s) { try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch (e) { /* private mode */ } }

  // quality presets: height, fps, kbps
  /** Ask the browser to play remote media as soon as it can (no extra buffering). */
  function lowLatency(receiver) {
    if (!receiver) return;
    try { if ('jitterBufferTarget' in receiver) receiver.jitterBufferTarget = 0; else if ('playoutDelayHint' in receiver) receiver.playoutDelayHint = 0; } catch (e) { /* not supported */ }
  }
  const QUALITY = { low: [360, 15, 300], medium: [540, 24, 800], high: [720, 30, 1500], hd: [1080, 30, 3000] };

  function later(p, q) { // does participant p sort after q? (the later joiner initiates)
    if (p.joined_at !== q.joined_at) return p.joined_at > q.joined_at;
    return p.peer_id > q.peer_id;
  }

  /**
   * const client = new NodeMeet.Client({ base: '/meet', token });
   * client.on('stream', ({ peerId, stream }) => ...);
   * await client.join();
   */
  class Client extends Emitter {
    constructor(opts) {
      super();
      opts = typeof opts === 'string' ? { token: opts } : (opts || {});
      const token = opts.token || tokenFromPage();
      if (!token) throw new Error('nodemeet: no token. Pass { token } (from your backend: meet.link(...)) or open a link with #token=...');
      this.token = token;
      this.base = opts.base != null ? opts.base : SCRIPT_BASE;
      this.name = opts.name || null;
      this.media = Object.assign({ audio: true, video: true }, opts.media || {});
      this.claims = decodeToken(this.token);
      this.roomId = opts.room || (this.claims && this.claims.room !== '*' ? this.claims.room : null);
      // ?room= lets a load balancer pin every member of a room to one server (SFU affinity)
      this.url = opts.url || (wsUrlFrom(this.base) + (this.roomId ? '?room=' + encodeURIComponent(this.roomId) : ''));
      this.reconnect = opts.reconnect !== false;
      this.settings = Object.assign({
        cameraId: null, microphoneId: null, speakerId: null, quality: 'auto',
        echoCancellation: true, noiseSuppression: true, autoGainControl: true,
        backgroundBlur: false, mirror: true, hideSelf: false,
      }, loadSettings(), opts.settings || {});
      this._reset();
      this.localStream = null;
      this.screenStream = null;
      this.audioMuted = !this.media.audio;
      this.videoMuted = !this.media.video;
      this._left = false;
    }

    _reset() {
      this.peerId = null;
      this.you = null;
      this.room = null;
      this.topology = 'p2p';
      this.iceServers = [];
      this.permissions = [];
      this.participants = new Map();
      this.publishers = new Set();
      this.mesh = new Map();
      this.sfuPub = null;
      this.sfuSubs = new Map();
      this.remoteStreams = new Map();
      this.polls = new Map(); this.myVotes = {}; this.questions = new Map(); this.board = [];
      this.breakouts = {}; this.recording = null; this.captions = { enabled: false };
    }

    get canPublish() { return !!(this.you && this.you.can_publish); }
    canPublishKind(kind) { return !!(this.you && this.you.publish && this.you.publish[kind]); }
    get inLobby() { return !!this._lobby; }
    get isHost() { return !!(this.you && this.you.role === 'host'); }
    can(perm) { return this.permissions.indexOf(perm) !== -1; }
    participantList() { return Array.from(this.participants.values()); }

    // ---- connection -------------------------------------------------------
    join() {
      this._left = false;
      return new Promise((resolve, reject) => {
        let settled = false;
        const ws = new WebSocket(this.url);
        this.ws = ws;
        ws.onopen = () => {
          this._attempts = 0;
          this._send({ type: 'join', token: this.token, room: this.roomId, name: this.name, device: deviceId() });
          clearInterval(this._ping);
          this._ping = setInterval(() => this._send({ type: 'ping', ts: Date.now() }), 20000);
        };
        ws.onmessage = (ev) => {
          let msg;
          try { msg = JSON.parse(ev.data); } catch (e) { return; }
          if (msg.type === 'welcome') {
            this._lobby = false;
            this._onWelcome(msg).then(() => { settled = true; resolve(this); }, reject);
          } else if (msg.type === 'lobby') {
            this._lobby = true;
            this.emit('lobby', msg);
          } else if (msg.type === 'lobby-message' || msg.type === 'lobby-position') {
            this.emit(msg.type, msg);
          } else if (msg.type === 'admitted') {
            this.emit('admitted', msg);
          } else if (msg.type === 'denied' || (msg.type === 'banned' && !this.peerId)) {
            this._left = true;
            this.emit(msg.type, msg);
            if (!settled) {
              settled = true;
              const e = new Error(msg.type === 'banned' ? 'You were blocked from this meeting' : 'The host did not let you in');
              e.code = msg.type; reject(e);
            }
          } else if (msg.type === 'error' && !settled && !this.peerId) {
            settled = true;
            const err = new Error(msg.message); err.code = msg.code;
            reject(err);
            this.emit('error', msg);
          } else {
            this._handle(msg);
          }
        };
        ws.onclose = () => {
          clearInterval(this._ping);
          const wasJoined = !!this.peerId;
          this._teardownMedia();
          this._reset();
          if (!settled) { settled = true; reject(new Error('connection closed')); }
          this.emit('disconnected', {});
          if (wasJoined && this.reconnect && !this._left) this._scheduleReconnect();
        };
      });
    }

    _scheduleReconnect() {
      this._attempts = (this._attempts || 0) + 1;
      if (this._attempts > 8) { this.emit('left', { reason: 'reconnect-failed' }); return; }
      const delay = Math.min(1000 * Math.pow(1.7, this._attempts), 15000);
      this.emit('reconnecting', { attempt: this._attempts, delay });
      setTimeout(() => { if (!this._left) this.join().catch(() => this._scheduleReconnect()); }, delay);
    }

    leave() {
      this._left = true;
      if (this.ws && this.ws.readyState <= 1) { this._send({ type: 'leave' }); this.ws.close(); }
      this._stopRecorder();
      this._stopCaptioner();
      this._teardownMedia();
      this._stopLocal();
      this._reset();
      this.emit('left', { reason: this._switching ? 'switching' : 'left' });
    }

    _send(msg) {
      if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(msg));
    }

    async _onWelcome(msg) {
      this.peerId = msg.peer_id;
      this.you = msg.you;
      this.room = msg.room;
      this.topology = msg.topology;
      this.iceServers = msg.ice_servers || [];
      this.permissions = msg.permissions || [];
      this.participants = new Map();
      (msg.room.participants || []).forEach((p) => { if (p.peer_id !== this.peerId) this.participants.set(p.peer_id, p); });
      this.publishers = new Set(msg.publishers || []);
      this.roles = msg.roles || [];
      this.lobby = msg.lobby || [];
      this.branding = msg.branding || {};
      if (this.you.audio_muted) this.audioMuted = true;
      if (this.you.video_muted) this.videoMuted = true;
      if (this.canPublish) await this._ensureLocalMedia();
      else this._stopLocal();
      if (this.canPublish) this._sendState();
      this.emit('joined', { you: this.you, room: this.room, peerId: this.peerId });
      this.emit('participants', this.participantList());
      (msg.chat || []).forEach((m) => this.emit('chat', Object.assign({ history: true }, m)));
      await this._applyTopology();
    }

    _handle(msg) {
      switch (msg.type) {
        case 'peer-joined':
          this.participants.set(msg.participant.peer_id, msg.participant);
          this.emit('participant-joined', msg.participant);
          this.emit('participants', this.participantList());
          break;
        case 'peer-left':
          this._dropPeer(msg.peer_id);
          this.participants.delete(msg.peer_id);
          this.emit('participant-left', { peerId: msg.peer_id });
          this.emit('participants', this.participantList());
          break;
        case 'peer-updated': this._onPeerUpdated(msg.participant); break;
        case 'signal': this._onSignal(msg.from, msg.data || {}); break;
        case 'chat': this.emit('chat', msg.message); break;
        case 'topology': this._onTopology(msg); break;
        case 'publisher':
          if (msg.publishing) { this.publishers.add(msg.peer_id); if (this.topology === 'sfu') this._sfuSubscribe(msg.peer_id); }
          else this.publishers.delete(msg.peer_id);
          break;
        case 'sfu-publish-answer':
          if (this.sfuPub) this.sfuPub.setRemoteDescription({ type: msg.sdpType, sdp: msg.sdp }).catch((e) => this.emit('error', { code: 'sfu', message: String(e) }));
          break;
        case 'sfu-subscribe-answer': {
          const sub = this.sfuSubs.get(msg.publisher);
          if (sub) sub.pc.setRemoteDescription({ type: msg.sdpType, sdp: msg.sdp }).catch((e) => this.emit('error', { code: 'sfu', message: String(e) }));
          break;
        }
        case 'force-mute': this.setAudioMuted(true, true); this.emit('force-mute', msg); break;
        case 'force-video-off': this.setVideoMuted(true, true); this.emit('force-video-off', msg); break;
        case 'force-stop-screen': this._stopScreen(false); this.emit('force-stop-screen', msg); break;
        case 'unmute-request': this.emit('unmute-request', msg); break;
        case 'unmute-allowed': this.emit('unmute-allowed', msg); break;
        case 'video-allowed': this.emit('video-allowed', msg); break;
        case 'permissions': this._onPermissions(msg); break;
        case 'kicked': this._left = true; this.emit('kicked', msg); break;
        case 'banned': this._left = true; this.emit('banned', msg); break;
        case 'lobby-update': this.lobby = msg.lobby || []; this.emit('lobby-update', this.lobby); break;
        case 'chat-deleted': this.emit('chat-deleted', msg); break;
        case 'chat-pinned': if (this.room) this.room.pinned = msg.message; this.emit('chat-pinned', msg.message); break;
        case 'reaction': this.emit('reaction', msg); break;
        case 'ban-attempt': this.emit('ban-attempt', msg); break;
        case 'features':
          this.polls = new Map((msg.polls || []).map((p) => [p.id, p])); this.myVotes = msg.my_votes || {};
          this.questions = new Map((msg.questions || []).map((q) => [q.id, q])); this.board = msg.board || [];
          this.breakouts = msg.breakouts || {}; this.recording = msg.recording || null; this.captions = msg.captions || { enabled: false };
          this.emit('features', msg); this._captionsChanged(); break;
        case 'poll': this.polls.set(msg.poll.id, msg.poll); this.emit('poll', msg.poll); break;
        case 'poll-removed': this.polls.delete(msg.id); this.emit('poll-removed', msg); break;
        case 'question': this.questions.set(msg.question.id, msg.question); this.emit('question', msg.question); break;
        case 'question-removed': this.questions.delete(msg.id); this.emit('question-removed', msg); break;
        case 'board':
          if (msg.op === 'add') this.board.push(msg.stroke);
          else if (msg.op === 'remove') this.board = this.board.filter((x) => x.id !== msg.id);
          else if (msg.op === 'clear') this.board = [];
          this.emit('board', msg); break;
        case 'breakouts': this.breakouts = msg.breakouts || {}; this.emit('breakouts', this.breakouts); break;
        case 'breakout-assign': this.emit('breakout-assign', msg); break;
        case 'breakout-closing': this.emit('breakout-closing', msg); break;
        case 'breakout-return': this.emit('breakout-return', msg); if (this.autoReturn !== false) this.switchRoom(msg.room, msg.token); break;
        case 'announcement': this.emit('announcement', msg); break;
        case 'recording': this.recording = msg.recording; this.emit('recording', msg.recording); if (!msg.recording) this._stopRecorder(); else this._maybeStartRecorder(); break;
        case 'captions-state': this.captions = msg.captions || { enabled: false }; this.emit('captions-state', this.captions); this._captionsChanged(); break;
        case 'caption': this.emit('caption', msg); break;
        case 'lobby-message': this.emit('lobby-message', msg); break;
        case 'lobby-position': this.emit('lobby-position', msg); break;
        case 'spotlight': if (this.room) this.room.spotlight = msg.peer_id; this.emit('spotlight', msg); break;
        case 'room-closed': this._left = true; this.emit('room-closed', msg); break;
        case 'room-updated': this.room = msg.room; this.emit('room-updated', msg.room); break;
        case 'error':
          if (msg.code === 'not_publishing') break;
          this.emit('error', msg); break;
        case 'pong': break;
        default: this.emit('message', msg);
      }
    }

    _onPeerUpdated(p) {
      if (p.peer_id === this.peerId) {
        const before = JSON.stringify((this.you && this.you.publish) || {});
        this.you = p;
        if (before !== JSON.stringify(p.publish || {})) this._publishRightsChanged();
        this.emit('you-updated', p);
        return;
      }
      const before = this.participants.get(p.peer_id);
      this.participants.set(p.peer_id, p);
      if (before && before.can_publish !== p.can_publish && this.topology === 'p2p') this._ensureMesh();
      if (before && JSON.stringify(before.receive) !== JSON.stringify(p.receive) && this.mesh.has(p.peer_id)) {
        this._attachLocal(this.mesh.get(p.peer_id), p.peer_id);
      }
      this.emit('participant-updated', p);
      this.emit('participants', this.participantList());
    }

    _onPermissions(msg) {
      this.permissions = msg.permissions || this.permissions;
      if (this.you) {
        const before = JSON.stringify(this.you.publish || {});
        this.you = Object.assign({}, this.you, msg.you || { can_publish: !!msg.can_publish });
        if (before !== JSON.stringify(this.you.publish || {})) this._publishRightsChanged();
      }
      this.emit('permissions', msg);
    }

    async _publishRightsChanged() {
      if (this.canPublish) {
        this._stopLocal();  // re-capture with the kinds we may now send
        await this._ensureLocalMedia();
        this._sendState();
        if (this.topology === 'sfu') this._sfuPublish();
        else { this.mesh.forEach((e, id) => this._attachLocal(e, id)); this._ensureMesh(); }
      } else {
        this._stopScreen(false);
        this._stopLocal();
        this.mesh.forEach((e, id) => this._attachLocal(e, id));
        if (this.sfuPub) { this.sfuPub.close(); this.sfuPub = null; }
      }
    }

    _onTopology(msg) {
      (msg.participants || []).forEach((p) => {
        if (p.peer_id === this.peerId) this.you = p; else this.participants.set(p.peer_id, p);
      });
      if (msg.topology === this.topology) return;
      this.topology = msg.topology;
      if (msg.topology === 'sfu') this.publishers = new Set();
      this.emit('topology', { topology: this.topology });
      this._applyTopology();
    }

    // ---- local media -------------------------------------------------------
    /** Effective capture limits: user's quality choice capped by the role. */
    videoLimits() {
      const attrs = (this.you && this.you.attributes) || {};
      let [h, fps, kbps] = QUALITY[this.settings.quality] || QUALITY.high;
      // auto = 720p30: 1080p is only sent when someone picks it. Software-encoding 1080p on a laptop (more so with
      // two tabs on one PC) overloads the CPU, and an overloaded encoder queues frames: that is the lag people see.
      if (this.settings.quality === 'auto') [h, fps, kbps] = QUALITY.high;
      if (!this.can('media.hd')) { h = Math.min(h, 720); kbps = Math.min(kbps, 1500); }
      if (attrs.max_video_height) h = Math.min(h, Number(attrs.max_video_height));
      if (attrs.max_fps) fps = Math.min(fps, Number(attrs.max_fps));
      if (attrs.max_bitrate_kbps) kbps = Math.min(kbps, Number(attrs.max_bitrate_kbps));
      return { height: h, fps, kbps };
    }

    _constraints() {
      const st = this.settings, lim = this.videoLimits();
      const audio = this.canPublishKind('audio') ? {
        echoCancellation: st.echoCancellation, noiseSuppression: st.noiseSuppression, autoGainControl: st.autoGainControl,
      } : false;
      if (audio && st.microphoneId) audio.deviceId = { exact: st.microphoneId };
      let video = false;
      if (this.canPublishKind('video')) {
        video = { height: { ideal: lim.height }, width: { ideal: Math.round(lim.height * 16 / 9) }, frameRate: { ideal: lim.fps, max: lim.fps } };
        if (st.cameraId) video.deviceId = { exact: st.cameraId };
        if (st.backgroundBlur) video.backgroundBlur = true;  // honoured where the browser/OS supports it
      }
      return { audio, video };
    }

    async _ensureLocalMedia() {
      if (this.localStream) return this.localStream;
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        this.emit('media-error', { message: 'getUserMedia is not available (HTTPS required)' });
        return null;
      }
      const c = this._constraints();
      const tries = [c, { audio: c.audio, video: false }, { audio: false, video: c.video }]
        .filter((t) => t.audio || t.video);
      for (const t of tries) {
        try { this.localStream = await navigator.mediaDevices.getUserMedia(t); break; } catch (e) { this._mediaErr = e; }
      }
      if (!this.localStream) {
        if (tries.length) this.emit('media-error', { message: String(this._mediaErr || 'no media') });
        this.localStream = new MediaStream();
      }
      this.localStream.getAudioTracks().forEach((t) => { t.enabled = !this.audioMuted; });
      this.localStream.getVideoTracks().forEach((t) => { t.enabled = !this.videoMuted; });
      this.emit('local-stream', { stream: this.localStream });
      if (this.captions && this.captions.enabled) this._captionsChanged();
      return this.localStream;
    }

    _stopLocal() {
      if (this.localStream) {
        this.localStream.getTracks().forEach((t) => t.stop());
        this.localStream = null;
        this.emit('local-stream', { stream: null });
      }
    }

    // ---- devices & settings --------------------------------------------------
    async listDevices() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return { cameras: [], microphones: [], speakers: [] };
      const all = await navigator.mediaDevices.enumerateDevices();
      const pick = (k) => all.filter((d) => d.kind === k).map((d, i) => ({ id: d.deviceId, label: d.label || (k + ' ' + (i + 1)) }));
      return { cameras: pick('videoinput'), microphones: pick('audioinput'), speakers: pick('audiooutput'),
        speakerSelection: typeof HTMLMediaElement !== 'undefined' && 'setSinkId' in HTMLMediaElement.prototype,
        blurSupported: !!(navigator.mediaDevices.getSupportedConstraints && navigator.mediaDevices.getSupportedConstraints().backgroundBlur) };
    }

    /** Change settings (cameraId, microphoneId, speakerId, quality, echoCancellation,
     *  noiseSuppression, autoGainControl, backgroundBlur, mirror, hideSelf). Live-applied. */
    async updateSettings(patch) {
      const before = Object.assign({}, this.settings);
      Object.assign(this.settings, patch || {});
      saveSettings(this.settings);
      const recapture = ['cameraId', 'microphoneId', 'quality', 'echoCancellation', 'noiseSuppression', 'autoGainControl', 'backgroundBlur']
        .some((k) => before[k] !== this.settings[k]);
      if (recapture && this.localStream && this.canPublish) await this._recapture();
      if (before.speakerId !== this.settings.speakerId) this.emit('speaker-changed', { speakerId: this.settings.speakerId });
      this.emit('settings', Object.assign({}, this.settings));
      return this.settings;
    }

    async _recapture() {
      const old = this.localStream;
      this.localStream = null;
      await this._ensureLocalMedia();
      if (old) old.getTracks().forEach((t) => t.stop());
      await this._replaceAll();
    }

    async _replaceAll() {
      const jobs = [];
      this.mesh.forEach((e, peerId) => jobs.push(this._attachLocal(e, peerId)));
      if (this.sfuPub) {
        this.sfuPub.getTransceivers().forEach((t) => {
          const kind = t.receiver && t.receiver.track && t.receiver.track.kind;
          if (kind === 'audio') jobs.push(t.sender.replaceTrack(this._audioTrack()));
        });
        if (this.sfuPubVideo) jobs.push(this.sfuPubVideo.sender.replaceTrack(this._videoTrack()).then(() => this._capSender(this.sfuPubVideo.sender)));
      }
      await Promise.all(jobs);
    }

    /** Route remote audio to the chosen speaker (Chrome/Edge/Firefox 116+). */
    async applySpeaker(el) {
      if (el && this.settings.speakerId && el.setSinkId) { try { await el.setSinkId(this.settings.speakerId); } catch (e) { /* not allowed */ } }
    }

    async _capSender(sender) {
      if (!sender || !sender.getParameters || !sender.track || sender.track.kind !== 'video') return;
      try {
        const p = sender.getParameters();
        if (!p.encodings || !p.encodings.length) return;     // not negotiated yet: called again once it is
        const lim = this.videoLimits();
        const screen = !!(this.screenStream && this.screenStream.getVideoTracks()[0] === sender.track);
        p.encodings[0].maxBitrate = lim.kbps * 1000;
        p.encodings[0].maxFramerate = lim.fps;
        // camera: when the CPU or network is short, drop resolution, never smoothness (movement stays live);
        // screen share: keep text sharp, drop frames instead
        try { sender.track.contentHint = screen ? 'detail' : 'motion'; } catch (e) { /* older browsers */ }
        try { await sender.setParameters({ ...p, degradationPreference: screen ? 'maintain-resolution' : 'maintain-framerate' }); }
        catch (e) { await sender.setParameters(p); }        // browsers without degradationPreference still get the caps
      } catch (e) { /* some browsers reject before negotiation */ }
    }

    _audioTrack() {
      if (this._mix && this.canPublishKind('audio')) return this._mix.track;
      return (this.canPublishKind('audio') && this.localStream && this.localStream.getAudioTracks()[0]) || null;
    }
    _videoTrack() {
      if (this.screenStream && this.canPublishKind('screen')) return this.screenStream.getVideoTracks()[0] || null;
      if (!this.canPublishKind('video')) return null;
      return (this.localStream && this.localStream.getVideoTracks()[0]) || null;
    }

    _sendState() {
      this._send({ type: 'state', audio_muted: this.audioMuted, video_muted: this.videoMuted,
        screen_sharing: !!this.screenStream });
    }

    _lockedOut(kind) {
      const you = this.you || {};
      if (kind === 'audio' && you.audio_locked && !this.can('audio.unmute_self')) return true;
      if (kind === 'video' && you.video_locked && !this.can('video.start_self')) return true;
      return false;
    }

    setAudioMuted(muted, forced) {
      if (!muted && !forced && this._lockedOut('audio')) { this.emit('error', { code: 'locked', message: 'A moderator muted you. Raise your hand to ask to speak.' }); return false; }
      this.audioMuted = !!muted;
      if (this.localStream) this.localStream.getAudioTracks().forEach((t) => { t.enabled = !muted; });
      if (!forced) this._sendState();
      if (forced && this.you) this.you.audio_locked = true;
      this.emit('local-state', this.localState());
      return true;
    }

    setVideoMuted(muted, forced) {
      if (!muted && !forced && this._lockedOut('video')) { this.emit('error', { code: 'locked', message: 'A moderator turned off your camera.' }); return false; }
      this.videoMuted = !!muted;
      if (this.localStream) this.localStream.getVideoTracks().forEach((t) => { t.enabled = !muted; });
      if (!forced) this._sendState();
      if (forced && this.you) this.you.video_locked = true;
      this.emit('local-state', this.localState());
      return true;
    }

    toggleAudio() { this.setAudioMuted(!this.audioMuted); return this.audioMuted; }
    toggleVideo() { this.setVideoMuted(!this.videoMuted); return this.videoMuted; }
    localState() { return { audioMuted: this.audioMuted, videoMuted: this.videoMuted, screenSharing: !!this.screenStream, handRaised: !!(this.you && this.you.hand_raised) }; }

    async startScreenShare() {
      if (!this.canPublishKind('screen')) throw new Error('screen sharing is not allowed for your role');
      if (this.screenStream) return this.screenStream;
      this.screenStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: this.can('screen.audio') });
      const track = this.screenStream.getVideoTracks()[0];
      track.onended = () => this.stopScreenShare();
      if (this.screenStream.getAudioTracks().length) this._mixScreenAudio();
      await this._replaceVideo();
      this._sendState();
      this.emit('screen-share', { active: true, stream: this.screenStream });
      this.emit('local-state', this.localState());
      return this.screenStream;
    }

    async stopScreenShare() { await this._stopScreen(true); }

    /** Mix tab/system audio from a screen share into the microphone track. */
    _mixScreenAudio() {
      try {
        const Ctx = global.AudioContext || global.webkitAudioContext;
        const ctx = new Ctx();
        const dest = ctx.createMediaStreamDestination();
        ctx.createMediaStreamSource(new MediaStream(this.screenStream.getAudioTracks())).connect(dest);
        const mic = this.localStream && this.localStream.getAudioTracks()[0];
        if (mic) ctx.createMediaStreamSource(new MediaStream([mic])).connect(dest);
        this._mix = { ctx, track: dest.stream.getAudioTracks()[0] };
        this._replaceAll();
      } catch (e) { this.emit('error', { code: 'screen_audio', message: String(e) }); }
    }

    async _stopScreen(notify) {
      if (!this.screenStream) return;
      this.screenStream.getTracks().forEach((t) => t.stop());
      this.screenStream = null;
      if (this._mix) { this._mix.ctx.close(); this._mix = null; await this._replaceAll(); }
      await this._replaceVideo();
      if (notify) this._sendState();
      this.emit('screen-share', { active: false, stream: null });
      this.emit('local-state', this.localState());
    }

    async _replaceVideo() {
      const jobs = [];
      this.mesh.forEach((e, peerId) => jobs.push(this._attachLocal(e, peerId)));
      if (this.sfuPubVideo) jobs.push(this.sfuPubVideo.sender.replaceTrack(this._videoTrack()).then(() => this._capSender(this.sfuPubVideo.sender)));
      await Promise.all(jobs);
    }

    // ---- chat, reactions, presence ----------------------------------------------
    sendChat(text, to) { if (text && String(text).trim()) this._send({ type: 'chat', text: String(text), to: to || undefined }); }
    deleteChat(id) { this._send({ type: 'chat-delete', id }); }
    pinChat(id) { this._send({ type: 'chat-pin', id: id || null }); }
    react(emoji) { this._send({ type: 'reaction', emoji }); }
    rename(name) { this._send({ type: 'rename', name }); }
    raiseHand(raised) { this._send({ type: 'raise-hand', raised: raised !== false }); }
    toggleHand() { const r = !(this.you && this.you.hand_raised); this.raiseHand(r); return r; }

    // ---- moderation (the server checks permissions and ranks) ---------------------
    moderate(action, target, extra) { this._send(Object.assign({ type: 'moderate', action, target }, extra || {})); }
    mute(peerId) { this.moderate('mute', peerId); }
    muteAll() { this.moderate('mute-all'); }
    askUnmute(peerId) { this.moderate('ask-unmute', peerId); }
    allowUnmute(peerId) { this.moderate('allow-unmute', peerId); }
    videoOff(peerId) { this.moderate('video-off', peerId); }
    allowVideo(peerId) { this.moderate('allow-video', peerId); }
    stopScreen(peerId) { this.moderate('stop-screen', peerId); }
    lowerHand(peerId) { this.moderate('lower-hand', peerId); }
    lowerAllHands() { this.moderate('lower-all-hands'); }
    renameParticipant(peerId, name) { this.moderate('rename', peerId, { name }); }
    kick(peerId) { this.moderate('kick', peerId); }
    ban(peerId) { this.moderate('ban', peerId); }
    unban(userId) { this.moderate('unban', null, { user_id: userId }); }
    promote(peerId) { this.moderate('promote', peerId); }
    demote(peerId) { this.moderate('demote', peerId); }
    setRole(peerId, role) { this.moderate('set-role', peerId, { role }); }
    spotlight(peerId) { this.moderate('spotlight', peerId || null); }
    admit(peerId) { this.moderate('admit', peerId); }
    deny(peerId) { this.moderate('deny', peerId); }
    admitAll() { this.moderate('admit-all'); }
    admitAlways(peerId) { this.moderate('admit-always', peerId); }   // admit + add to the join list
    block(peerId) { this.moderate('block', peerId); }               // deny from the waiting room + ban
    addToJoinList(peerId) { this.moderate('allow', peerId); }
    removeFromJoinList(peerId) { this.moderate('disallow', peerId); }
    setLobby(on) { this.moderate(on ? 'lobby-on' : 'lobby-off'); }
    setChatEnabled(on) { this.moderate(on ? 'chat-on' : 'chat-off'); }
    lockRoom(locked) { this.moderate(locked === false ? 'unlock' : 'lock'); }
    endMeeting() { this.moderate('end'); }
    setLobbyMessage(text) { this.moderate('lobby-message', null, { text }); }
    setLobbyMode(mode) { this.moderate('lobby-mode', null, { mode }); }   // 'manual' | 'until_host'
    ban(peerId, scope) { this.moderate('ban', peerId, scope ? { scope } : undefined); }  // scope: ['user','device','ip']

    // ---- polls, Q&A, whiteboard -----------------------------------------------------
    createPoll(question, options, opts) { this._send(Object.assign({ type: 'poll-create', question, options }, opts || {})); }
    vote(pollId, choices) { const c = Array.isArray(choices) ? choices : [choices]; this.myVotes[pollId] = c; this._send({ type: 'poll-vote', id: pollId, choices: c }); }
    closePoll(id) { this._send({ type: 'poll-close', id }); }
    deletePoll(id) { this._send({ type: 'poll-delete', id }); }
    ask(text, anonymous) { this._send({ type: 'qa-ask', text, anonymous: !!anonymous }); }
    upvote(id) { this._send({ type: 'qa-upvote', id }); }
    answer(id, opts) { this._send(Object.assign({ type: 'qa-answer', id }, opts || { answered: true })); }
    draw(stroke) { this._send({ type: 'board-draw', stroke }); }      // {points:[[x,y]...0..1], color, width, tool}
    undoStroke(id) { this._send({ type: 'board-undo', id: id || null }); }
    clearBoard() { this._send({ type: 'board-clear' }); }

    // ---- recording (browser-side compositor; the server stores the file) -----------------
    async _upload(path, body, type) {
      const res = await fetch((this.base || '') + path, { method: 'POST', body,
        headers: { 'X-Join-Token': this.token, 'Content-Type': type || 'application/octet-stream' } });
      if (!res.ok) throw new Error('upload failed: ' + res.status);
      return res.json().catch(() => ({}));
    }

    /** Start recording: everyone sees a REC indicator; this browser mixes and uploads. */
    startRecording(opts) {
      if (!this.can('recording.start')) throw new Error('your role may not record');
      if (typeof MediaRecorder === 'undefined') throw new Error('this browser cannot record');
      this._recOpts = Object.assign({ width: 1280, height: 720, fps: 25, timeslice: 4000 }, opts || {});
      this._wantRecord = true;
      this._send({ type: 'recording', action: 'start' });
    }
    stopRecording() { this._send({ type: 'recording', action: 'stop' }); }

    _maybeStartRecorder() {
      const rec = this.recording;
      if (!rec || !this._wantRecord || rec.by_peer !== this.peerId || this._rec) return;
      this._wantRecord = false;
      const o = this._recOpts, canvas = document.createElement('canvas');
      canvas.width = o.width; canvas.height = o.height;
      const g = canvas.getContext('2d');
      const videos = new Map();
      const videoFor = (id, stream) => {
        let v = videos.get(id);
        if (!v || v.srcObject !== stream) { v = document.createElement('video'); v.muted = true; v.playsInline = true; v.srcObject = stream; v.play().catch(() => {}); videos.set(id, v); }
        return v;
      };
      const Ctx = global.AudioContext || global.webkitAudioContext;
      const ac = new Ctx(), dest = ac.createMediaStreamDestination(), mixed = new Set();
      const mix = (stream) => { if (stream && !mixed.has(stream) && stream.getAudioTracks().length) { mixed.add(stream); ac.createMediaStreamSource(stream).connect(dest); } };
      const draw = () => {
        const tiles = [];
        if (this.localStream) tiles.push(['me', this.screenStream || this.localStream, (this.you && this.you.name) || 'me']);
        this.remoteStreams.forEach((st, id) => tiles.push([id, st, (this.participants.get(id) || {}).name || '']));
        tiles.forEach(([, st]) => mix(st));
        const spot = this.room && this.room.spotlight;
        const list = spot ? tiles.filter((t) => t[0] === spot).concat(tiles.filter((t) => t[0] !== spot)).slice(0, 1) : tiles;
        const n = Math.max(1, list.length), cols = Math.ceil(Math.sqrt(n)), rows = Math.ceil(n / cols);
        const w = o.width / cols, h = o.height / rows;
        g.fillStyle = '#111'; g.fillRect(0, 0, o.width, o.height);
        list.forEach(([id, st, name], i) => {
          const x = (i % cols) * w, y = Math.floor(i / cols) * h, v = videoFor(id, st);
          if (v.videoWidth) { const r = Math.min(w / v.videoWidth, h / v.videoHeight); const dw = v.videoWidth * r, dh = v.videoHeight * r; g.drawImage(v, x + (w - dw) / 2, y + (h - dh) / 2, dw, dh); }
          g.fillStyle = 'rgba(0,0,0,.55)'; g.fillRect(x + 8, y + h - 30, Math.min(w - 16, 12 + name.length * 8), 22);
          g.fillStyle = '#fff'; g.font = '14px sans-serif'; g.fillText(name, x + 14, y + h - 14);
        });
      };
      const timer = setInterval(draw, 1000 / o.fps);
      const stream = canvas.captureStream(o.fps);
      dest.stream.getAudioTracks().forEach((t) => stream.addTrack(t));
      const mime = ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm'].find((m) => MediaRecorder.isTypeSupported(m)) || '';
      const mr = new MediaRecorder(stream, mime ? { mimeType: mime, videoBitsPerSecond: 2500000 } : {});
      let seq = 0, queue = Promise.resolve();
      const id = rec.id;
      mr.ondataavailable = (ev) => {
        const final = mr.state === 'inactive';
        if (!ev.data || (!ev.data.size && !final)) return;
        const n = seq++;
        queue = queue.then(() => this._upload('/api/recordings/' + id + '/chunks?seq=' + n + (final ? '&final=1' : ''), ev.data, 'video/webm'))
          .then(() => { if (final) this.emit('recording-saved', { id }); })
          .catch((e) => this.emit('error', { code: 'recording_upload', message: String(e) }));
      };
      mr.start(o.timeslice);
      this._rec = { mr, timer, ac, id };
      this.emit('recording-started', { id });
    }

    _stopRecorder() {
      const r = this._rec;
      if (!r) return;
      this._rec = null;
      clearInterval(r.timer);
      try { r.mr.stop(); } catch (e) { /* already stopped */ }
      setTimeout(() => r.ac.close().catch(() => {}), 2000);
    }

    // ---- captions ----------------------------------------------------------------------------
    enableCaptions(enabled, opts) { this._send(Object.assign({ type: 'captions', enabled: enabled !== false }, opts || {})); }

    _captionsChanged() {
      const on = this.captions && this.captions.enabled && this.canPublishKind('audio') && this.localStream;
      if (!on) { this._stopCaptioner(); return; }
      if (this._cap) return;
      if (this.captions.engine === 'server') this._serverCaptions(); else this._browserCaptions();
    }

    _browserCaptions() {
      const SR = global.SpeechRecognition || global.webkitSpeechRecognition;
      if (!SR) { this.emit('error', { code: 'captions', message: 'This browser has no speech recognition; ask the host to use server captions.' }); return; }
      const rec = new SR();
      rec.continuous = true; rec.interimResults = true;
      if (this.captions.language) rec.lang = this.captions.language;
      rec.onresult = (ev) => {
        if (this.audioMuted) return;
        for (let i = ev.resultIndex; i < ev.results.length; i++) {
          const r = ev.results[i];
          if (r.isFinal || i === ev.results.length - 1) this._send({ type: 'caption-text', text: r[0].transcript, final: r.isFinal });
        }
      };
      rec.onend = () => { if (this._cap && this._cap.sr === rec) { try { rec.start(); } catch (e) { /* restarting */ } } };
      try { rec.start(); } catch (e) { return; }
      this._cap = { sr: rec };
    }

    _serverCaptions() {
      const track = this.localStream && this.localStream.getAudioTracks()[0];
      if (!track || typeof MediaRecorder === 'undefined') return;
      const stream = new MediaStream([track]);
      const mime = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/webm'].find((m) => MediaRecorder.isTypeSupported(m)) || '';
      const cycle = () => {  // a fresh recorder every few seconds = self-contained audio files
        if (!this._cap) return;
        const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : {});
        const parts = [];
        mr.ondataavailable = (ev) => { if (ev.data && ev.data.size) parts.push(ev.data); };
        mr.onstop = () => {
          if (parts.length && !this.audioMuted) {
            const blob = new Blob(parts, { type: mime || 'audio/webm' });
            this._upload('/api/rooms/' + encodeURIComponent(this.roomId || (this.room && this.room.id)) + '/captions/audio', blob, blob.type).catch(() => {});
          }
          cycle();
        };
        mr.start();
        this._cap.mr = mr;
        setTimeout(() => { if (mr.state !== 'inactive') mr.stop(); }, 5000);
      };
      this._cap = { server: true };
      cycle();
    }

    _stopCaptioner() {
      const c = this._cap;
      this._cap = null;
      if (!c) return;
      if (c.sr) try { c.sr.stop(); } catch (e) { /* ignore */ }
      if (c.mr && c.mr.state !== 'inactive') try { c.mr.stop(); } catch (e) { /* ignore */ }
    }

    // ---- breakout rooms ---------------------------------------------------------------
    breakout(action, data) { this._send(Object.assign({ type: 'breakout', action }, data || {})); }
    async switchRoom(roomId, token) {
      this._switching = true;
      this.leave();
      this._reset();
      this._left = false;
      this.roomId = roomId; this.token = token; this.claims = decodeToken(token);
      this.url = wsUrlFrom(this.base) + '?room=' + encodeURIComponent(roomId);
      this.emit('switching', { room: roomId });
      try { await this.join(); } finally { this._switching = false; }
      this.emit('switched', { room: roomId });
    }

    _setRemote(peerId, stream) {
      this.remoteStreams.set(peerId, stream);
      this.emit('stream', { peerId, stream, participant: this.participants.get(peerId) });
    }

    _dropPeer(peerId) {
      const e = this.mesh.get(peerId);
      if (e) { e.pc.close(); this.mesh.delete(peerId); }
      const s = this.sfuSubs.get(peerId);
      if (s) { s.pc.close(); this.sfuSubs.delete(peerId); this._send({ type: 'sfu-unsubscribe', publisher: peerId }); }
      this.publishers.delete(peerId);
      if (this.remoteStreams.delete(peerId)) this.emit('stream-removed', { peerId });
    }

    _teardownMedia() {
      Array.from(this.mesh.keys()).concat(Array.from(this.sfuSubs.keys())).forEach((id) => this._dropPeer(id));
      if (this.sfuPub) { this.sfuPub.close(); this.sfuPub = null; this.sfuPubVideo = null; }
    }

    // ---- topology ------------------------------------------------------------
    async _applyTopology() {
      if (this.topology === 'sfu') {
        Array.from(this.mesh.keys()).forEach((id) => this._dropPeer(id));
        if (this.canPublish) await this._sfuPublish();
        this.publishers.forEach((id) => this._sfuSubscribe(id));
      } else {
        Array.from(this.sfuSubs.keys()).forEach((id) => this._dropPeer(id));
        if (this.sfuPub) { this.sfuPub.close(); this.sfuPub = null; this.sfuPubVideo = null; }
        this._ensureMesh();
      }
    }

    _ensureMesh() {
      if (this.topology !== 'p2p' || !this.you) return;
      this.participants.forEach((p) => {
        const wanted = this.canPublish || p.can_publish;
        if (wanted && !this.mesh.has(p.peer_id) && later(this.you, p)) this._meshOffer(p.peer_id);
      });
    }

    // ---- peer-to-peer mesh ---------------------------------------------------
    _meshEntry(peerId) {
      let e = this.mesh.get(peerId);
      if (e) return e;
      const pc = new RTCPeerConnection({ iceServers: this.iceServers });
      e = { pc, stream: new MediaStream(), queue: [], audio: null, video: null };
      pc.onicecandidate = (ev) => {
        if (ev.candidate) this._send({ type: 'signal', to: peerId, data: { candidate: ev.candidate } });
      };
      pc.ontrack = (ev) => {
        lowLatency(ev.receiver);
        e.stream.getTracks().filter((t) => t.kind === ev.track.kind).forEach((t) => e.stream.removeTrack(t));
        e.stream.addTrack(ev.track);
        this._setRemote(peerId, e.stream);
      };
      pc.onconnectionstatechange = () => {
        this.emit('connection-state', { peerId, state: pc.connectionState });
        if (pc.connectionState === 'failed' && e.initiator) {
          pc.restartIce && pc.restartIce();
          pc.createOffer({ iceRestart: true }).then((o) => pc.setLocalDescription(o)).then(() =>
            this._send({ type: 'signal', to: peerId, data: { sdp: pc.localDescription } })).catch(() => {});
        }
      };
      this.mesh.set(peerId, e);
      return e;
    }

    async _attachLocal(e, peerId) {
      // Respect what the other person may receive (their role's *.subscribe permissions).
      const p = this.participants.get(peerId) || {};
      const rx = p.receive || { audio: true, video: true, screen: true };
      const sharing = !!this.screenStream;
      if (e.audio) await e.audio.sender.replaceTrack(rx.audio ? this._audioTrack() : null);
      if (e.video) {
        await e.video.sender.replaceTrack((sharing ? rx.screen : rx.video) ? this._videoTrack() : null);
        await this._capSender(e.video.sender);
      }
    }

    async _meshOffer(peerId) {
      const e = this._meshEntry(peerId);
      e.initiator = true;
      e.audio = e.pc.addTransceiver('audio', { direction: 'sendrecv' });
      e.video = e.pc.addTransceiver('video', { direction: 'sendrecv' });
      await this._attachLocal(e, peerId);
      await e.pc.setLocalDescription(await e.pc.createOffer());
      this._send({ type: 'signal', to: peerId, data: { sdp: e.pc.localDescription } });
    }

    async _onSignal(from, data) {
      try {
        if (data.sdp) {
          const e = this._meshEntry(from);
          const pc = e.pc;
          if (data.sdp.type === 'offer') {
            await pc.setRemoteDescription(data.sdp);
            const ts = pc.getTransceivers();
            e.audio = e.audio || ts.find((t) => t.receiver.track && t.receiver.track.kind === 'audio') || null;
            e.video = e.video || ts.find((t) => t.receiver.track && t.receiver.track.kind === 'video') || null;
            [e.audio, e.video].forEach((t) => { if (t) t.direction = 'sendrecv'; });
            await this._attachLocal(e, from);
            await pc.setLocalDescription(await pc.createAnswer());
            this._send({ type: 'signal', to: from, data: { sdp: pc.localDescription } });
            if (e.video) await this._capSender(e.video.sender);   // encodings exist only now (0.9.4)
          } else if (data.sdp.type === 'answer' && pc.signalingState === 'have-local-offer') {
            await pc.setRemoteDescription(data.sdp);
            // caps set before negotiation are refused by Chrome: set them now (0.9.4)
            if (e.video) await this._capSender(e.video.sender);
          }
          while (e.queue.length) await pc.addIceCandidate(e.queue.shift()).catch(() => {});
        } else if (data.candidate) {
          const e = this._meshEntry(from);
          if (e.pc.remoteDescription) await e.pc.addIceCandidate(data.candidate).catch(() => {});
          else e.queue.push(data.candidate);
        }
      } catch (err) {
        this.emit('error', { code: 'webrtc', message: String(err), peerId: from });
      }
    }

    // ---- SFU -------------------------------------------------------------------
    async _sfuPublish() {
      if (this.sfuPub) { this.sfuPub.close(); }
      const pc = new RTCPeerConnection({ iceServers: this.iceServers });
      this.sfuPub = pc;
      pc.addTransceiver('audio', { direction: 'sendonly' }).sender.replaceTrack(this._audioTrack());
      this.sfuPubVideo = pc.addTransceiver('video', { direction: 'sendonly' });
      await this.sfuPubVideo.sender.replaceTrack(this._videoTrack());
      await pc.setLocalDescription(await pc.createOffer());
      this._capSender(this.sfuPubVideo.sender);
      await waitForIce(pc);
      if (this.sfuPub !== pc) return;
      this._send({ type: 'sfu-publish', sdp: pc.localDescription.sdp, sdpType: pc.localDescription.type });
    }

    async _sfuSubscribe(publisherId) {
      if (publisherId === this.peerId || this.topology !== 'sfu') return;
      const old = this.sfuSubs.get(publisherId);
      if (old) old.pc.close();
      const pc = new RTCPeerConnection({ iceServers: this.iceServers });
      const stream = new MediaStream();
      const entry = { pc, stream };
      this.sfuSubs.set(publisherId, entry);
      pc.addTransceiver('audio', { direction: 'recvonly' });
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.ontrack = (ev) => { lowLatency(ev.receiver); stream.addTrack(ev.track); this._setRemote(publisherId, stream); };
      await pc.setLocalDescription(await pc.createOffer());
      await waitForIce(pc);
      if (this.sfuSubs.get(publisherId) !== entry) return;
      this._send({ type: 'sfu-subscribe', publisher: publisherId, sdp: pc.localDescription.sdp, sdpType: pc.localDescription.type });
    }
  }

  // ---- REST helpers (booking) --------------------------------------------------
  async function api(base, path, opts) {
    const res = await fetch((base || '') + path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts || {}));
    const data = res.status === 204 ? null : await res.json().catch(() => null);
    if (!res.ok) { const e = new Error((data && data.message) || res.statusText); e.status = res.status; e.data = data; throw e; }
    return data;
  }

  const Booking = {
    host(base, hostId) { return api(base, '/api/hosts/' + encodeURIComponent(hostId) + '/availability'); },
    slots(base, hostId, q) {
      const qs = new URLSearchParams(Object.entries(q || {}).filter(([, v]) => v != null)).toString();
      return api(base, '/api/hosts/' + encodeURIComponent(hostId) + '/slots' + (qs ? '?' + qs : ''));
    },
    book(base, body) { return api(base, '/api/bookings', { method: 'POST', body: JSON.stringify(body) }); },
    get(base, id, t) { return api(base, '/api/bookings/' + encodeURIComponent(id) + '?t=' + encodeURIComponent(t)); },
    cancel(base, id, t, reason) { return api(base, '/api/bookings/' + encodeURIComponent(id) + '/cancel', { method: 'POST', body: JSON.stringify({ t, reason }) }); },
    reschedule(base, id, t, start) { return api(base, '/api/bookings/' + encodeURIComponent(id) + '/reschedule', { method: 'POST', body: JSON.stringify({ t, start }) }); },
  };

  // Push notifications: await NodeMeet.push.enable({ token })  (token = any join token of this user)
  const push = {
    supported() { return !!(global.navigator && 'serviceWorker' in global.navigator && 'PushManager' in global && 'Notification' in global); },
    async enable(opts) {
      opts = opts || {};
      const base = opts.base != null ? opts.base : SCRIPT_BASE;
      const token = opts.token || tokenFromPage();
      if (!push.supported()) throw new Error('nodemeet: push is not supported in this browser (on iOS, add the site to the home screen first)');
      if (!token) throw new Error('nodemeet: push.enable needs { token }');
      const perm = await global.Notification.requestPermission();
      if (perm !== 'granted') throw new Error('nodemeet: notifications were not allowed');
      const keyRes = await (await fetch(base + '/api/push/key')).json();
      const reg = await global.navigator.serviceWorker.register(opts.serviceWorker || (base + '/static/nodemeet-sw.js'));
      await global.navigator.serviceWorker.ready;
      const raw = global.atob(keyRes.public_key.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - keyRes.public_key.length % 4) % 4));
      const key = Uint8Array.from(raw, (c) => c.charCodeAt(0));
      const sub = (await reg.pushManager.getSubscription()) || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
      const res = await fetch(base + '/api/push/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, subscription: sub.toJSON() }) });
      if (!res.ok) throw new Error('nodemeet: push subscribe failed (' + res.status + ')');
      return sub;
    },
    async disable(opts) {
      opts = opts || {};
      const base = opts.base != null ? opts.base : SCRIPT_BASE;
      const reg = await global.navigator.serviceWorker.getRegistration(opts.serviceWorker || (base + '/static/nodemeet-sw.js'));
      const sub = reg && await reg.pushManager.getSubscription();
      if (!sub) return false;
      await fetch(base + '/api/push/unsubscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: opts.token || tokenFromPage(), subscription: sub.toJSON() }) });
      return sub.unsubscribe();
    },
  };

  const NodeMeet = { push, Client, Emitter, Booking, api, decodeToken, wsUrlFrom, version: '0.9.5', QUALITY, base: SCRIPT_BASE, tokenFromPage };
  global.NodeMeet = Object.assign(global.NodeMeet || {}, NodeMeet);
  if (typeof module !== 'undefined' && module.exports) module.exports = NodeMeet;
})(typeof window !== 'undefined' ? window : globalThis);
