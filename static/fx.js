// 互动特效：声音全部用 Web Audio 实时合成（不需要音效文件），画面用全屏 canvas 粒子加飘字。
const KTVFX = (() => {
  const MAX_PARTICLES = 2500;  // 很多人同时狂点时，超出的旧粒子直接丢掉
  const MAX_VOICES = 8;        // 同时发声的音效上限，再多只放画面不放声音
  const MAX_FLOATERS = 40;
  const COLORS = ["#ff4f8b", "#ffd23f", "#3ecf8e", "#4fc3ff", "#b388ff", "#ff8a3d", "#ffffff"];

  let canvas, ctx, layer, parts = [], rockets = [], last = 0;
  let actx = null, master = null, noise = null, voices = 0, volume = 1;

  // ------------------------------------------------------------ 画面
  function init() {
    const style = document.createElement("style");
    style.textContent = `
      #fx { position:fixed; inset:0; width:100%; height:100%; pointer-events:none; z-index:6; }
      #floaters { position:fixed; inset:0; pointer-events:none; z-index:7; overflow:hidden; }
      .floater { position:absolute; bottom:-12vh; display:flex; flex-direction:column; align-items:center;
                 animation:fx-float 3.6s ease-out forwards; text-shadow:0 2px 8px #000a; }
      .floater .emo { font-size:8vh; line-height:1; }
      .floater .who { font-size:2.4vh; font-weight:700; background:#000a; padding:2px 10px; border-radius:10px; margin-top:4px; }
      @keyframes fx-float { 0% { transform:translateY(0) scale(.6); opacity:0; } 12% { opacity:1; transform:translateY(-12vh) scale(1.1); }
                            100% { transform:translateY(-75vh) scale(1); opacity:0; } }
      .bigpop { position:absolute; font-size:22vh; line-height:1; transform:translate(-50%,-50%); animation:fx-pop 1.6s ease-out forwards;
                filter:drop-shadow(0 0 24px #ffd23f); }
      @keyframes fx-pop { 0% { transform:translate(-50%,-50%) scale(0) rotate(-30deg); opacity:0; }
                          25% { transform:translate(-50%,-50%) scale(1.35) rotate(8deg); opacity:1; }
                          40% { transform:translate(-50%,-50%) scale(1) rotate(0); } 80% { opacity:1; }
                          100% { transform:translate(-50%,-60%) scale(1); opacity:0; } }
      .mug { position:absolute; top:42%; font-size:16vh; line-height:1; animation:fx-mug-l 1.6s ease-in-out forwards; }
      .mug.r { animation-name:fx-mug-r; }
      /* 🍺 的把手只在一侧：左杯镜像翻转，让两杯都把手朝外、杯身朝里相碰。
         镜像后 rotate 的视觉方向会反过来，所以左杯写负角度，看起来仍是杯口朝中间倾斜。
         每个关键帧都写全 transform，否则淡出时会插值到无镜像，杯子被压扁翻面。 */
      @keyframes fx-mug-l { 0% { left:-15%; transform:scaleX(-1) rotate(0); } 35% { left:36%; transform:scaleX(-1) rotate(-20deg); }
                            45% { left:34%; transform:scaleX(-1) rotate(-5deg); } 80% { opacity:1; }
                            100% { left:30%; transform:scaleX(-1) rotate(-5deg); opacity:0; } }
      @keyframes fx-mug-r { 0% { left:105%; transform:rotate(0); } 35% { left:52%; transform:rotate(-20deg); }
                            45% { left:54%; transform:rotate(-5deg); } 80% { opacity:1; }
                            100% { left:58%; transform:rotate(-5deg); opacity:0; } }`;
    document.head.append(style);
    canvas = Object.assign(document.createElement("canvas"), { id: "fx" });
    layer = Object.assign(document.createElement("div"), { id: "floaters" });
    document.body.append(canvas, layer);
    ctx = canvas.getContext("2d");
    resize();
    addEventListener("resize", resize);
    requestAnimationFrame(loop);
  }

  function resize() {
    const dpr = devicePixelRatio || 1;
    canvas.width = innerWidth * dpr;
    canvas.height = innerHeight * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function add(p) {
    parts.push(p);
    if (parts.length > MAX_PARTICLES) parts.splice(0, parts.length - MAX_PARTICLES);
  }

  const rand = (a, b) => a + Math.random() * (b - a);

  // 礼花：从左下或右下角斜着往屏幕中间喷彩纸
  function confetti(side) {
    const left = side === "left";
    for (let i = 0; i < 110; i++) {
      const angle = (left ? -60 : -120) * Math.PI / 180 + rand(-0.3, 0.3);
      const speed = rand(650, 1250);
      add({ kind: "paper", x: left ? 0 : innerWidth, y: innerHeight, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed,
            g: 950, drag: 0.25, life: 0, ttl: rand(2.6, 3.8), color: COLORS[i % COLORS.length],
            w: rand(8, 15), h: rand(4, 8), rot: rand(0, 6.3), vr: rand(-12, 12), wobble: rand(0, 6.3) });
    }
  }

  // 烟花：先升空，速度减到接近 0 时在空中炸开
  function launch() {
    const x = innerWidth * rand(0.18, 0.82);
    const height = innerHeight * rand(0.55, 0.8);
    const g = 650, vy = -Math.sqrt(2 * g * height);
    rockets.push({ x, y: innerHeight, vx: rand(-40, 40), vy, g, hue: rand(0, 360) });
    sound.whoosh(-vy / g);
  }

  function explode(r) {
    const n = 130;
    for (let i = 0; i < n; i++) {
      const angle = (i / n) * Math.PI * 2 + rand(-0.05, 0.05);
      const speed = rand(120, 430);
      add({ kind: "spark", x: r.x, y: r.y, vx: Math.cos(angle) * speed, vy: Math.sin(angle) * speed,
            g: 140, drag: 1.6, life: 0, ttl: rand(1.1, 1.9), color: `hsl(${(r.hue + rand(-25, 25)) % 360},100%,${rand(55, 75)}%)`,
            size: rand(1.8, 3.2), flicker: Math.random() < 0.3 });
    }
    sound.boom();
  }

  const EMOJI_FONT = '"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif';
  function emoji(text, x, y, o) {
    add(Object.assign({ kind: "emoji", text, x, y, vx: 0, vy: 0, g: 0, drag: 0, life: 0, ttl: 3, size: 48,
                        rot: 0, vr: 0, sway: 0, phase: rand(0, 6.3) }, o));
  }

  function likeBurst() {  // 点赞：屏幕中间一带弹出一个大拇指，再炸开一圈小拇指
    const x = rand(0.3, 0.7), y = rand(0.35, 0.6);
    bigPop("👍", x, y);
    setTimeout(() => {
      for (let i = 0; i < 16; i++) {
        const a = (i / 16) * Math.PI * 2, v = rand(380, 620);
        emoji(i % 4 ? "👍" : "✨", innerWidth * x, innerHeight * y, { vx: Math.cos(a) * v, vy: Math.sin(a) * v - 200, g: 900, drag: 0.6,
              ttl: rand(1.3, 1.8), size: rand(34, 56), vr: rand(-4, 4) });
      }
    }, 380);
  }

  function hearts() {  // 比心：一串爱心从底部左右摇摆着往上飘
    for (let i = 0; i < 20; i++) {
      setTimeout(() => emoji(["❤️", "💖", "💕", "💗", "💘"][i % 5], innerWidth * rand(0.1, 0.9), innerHeight + 40,
        { vy: -rand(220, 420), drag: 0.1, ttl: rand(3.5, 4.5), size: rand(40, 76), sway: rand(30, 70) }), i * 90);
    }
  }

  function laughs() {  // 哈哈：笑脸从底部蹦起来再掉下去
    for (let i = 0; i < 12; i++) {
      setTimeout(() => emoji(["😂", "🤣", "😆", "😹"][i % 4], innerWidth * rand(0.08, 0.92), innerHeight + 50,
        { vx: rand(-120, 120), vy: -rand(950, 1350), g: 1700, ttl: 2.2, size: rand(56, 96), vr: rand(-5, 5) }), i * 110);
    }
  }

  function flowers() {  // 送花：花瓣从屏幕顶上飘落
    for (let i = 0; i < 26; i++) {
      setTimeout(() => emoji(["🌹", "🌸", "🌺", "💐", "🌷"][i % 5], innerWidth * rand(0.03, 0.97), -50,
        { vy: rand(110, 230), g: 25, ttl: rand(5, 7), size: rand(36, 64), vr: rand(-1.5, 1.5), sway: rand(25, 60) }), i * 120);
    }
  }

  function cheers() {  // 干杯：两杯啤酒从两边滑过来碰杯，碰上时冒金色火花
    for (const side of ["", " r"]) {
      const mug = Object.assign(document.createElement("div"), { className: "mug" + side, textContent: "🍺" });
      mug.addEventListener("animationend", () => mug.remove());
      layer.append(mug);
    }
    setTimeout(() => {
      const x = innerWidth * 0.5, y = innerHeight * 0.45;
      for (let i = 0; i < 70; i++) {
        const a = rand(0, Math.PI * 2), v = rand(150, 480);
        add({ kind: "spark", x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 120, g: 500, drag: 1.2, life: 0, ttl: rand(0.6, 1.1),
              color: `hsl(${rand(38, 52)},100%,${rand(55, 80)}%)`, size: rand(1.8, 3) });
      }
      emoji("🎉", x, y - 40, { vy: -160, ttl: 1.4, size: 90 });
      sound.clink();
    }, 560);  // 与 CSS 动画里两杯相碰的时刻（35%）对齐
  }

  function bigPop(text, x, y) {
    const el = Object.assign(document.createElement("div"), { className: "bigpop", textContent: text });
    el.style.left = `${x * 100}%`; el.style.top = `${y * 100}%`;
    el.addEventListener("animationend", () => el.remove());
    layer.append(el);
  }

  function loop(now) {
    const dt = Math.min(0.05, (now - last) / 1000 || 0);
    last = now;
    ctx.clearRect(0, 0, innerWidth, innerHeight);

    for (let i = rockets.length - 1; i >= 0; i--) {
      const r = rockets[i];
      r.vy += r.g * dt; r.x += r.vx * dt; r.y += r.vy * dt;
      add({ kind: "spark", x: r.x, y: r.y, vx: rand(-20, 20), vy: rand(20, 60), g: 0, drag: 2, life: 0, ttl: 0.45,
            color: `hsl(${r.hue},100%,80%)`, size: 1.8 });  // 升空的尾焰
      if (r.vy > -40) { explode(r); rockets.splice(i, 1); }
    }

    ctx.globalCompositeOperation = "lighter";
    for (let i = parts.length - 1; i >= 0; i--) {
      const p = parts[i];
      p.life += dt;
      if (p.life >= p.ttl) { parts.splice(i, 1); continue; }
      const k = Math.exp(-p.drag * dt);
      p.vx *= k; p.vy = p.vy * k + p.g * dt;
      p.x += p.vx * dt; p.y += p.vy * dt;
      const fade = Math.min(1, (p.ttl - p.life) / 0.6);
      if (p.kind === "spark") {
        ctx.globalAlpha = fade * (p.flicker && Math.random() < 0.4 ? 0.3 : 1);
        ctx.fillStyle = p.color;
        ctx.beginPath(); ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2); ctx.fill();
      }
    }
    ctx.globalCompositeOperation = "source-over";
    for (const p of parts) {
      if (p.kind !== "paper") continue;
      p.rot += p.vr * dt;
      p.wobble += dt * 8;
      ctx.globalAlpha = Math.min(1, (p.ttl - p.life) / 0.6);
      ctx.fillStyle = p.color;
      ctx.save();
      ctx.translate(p.x + Math.sin(p.wobble) * 6, p.y);  // 左右飘摆，像纸片
      ctx.rotate(p.rot);
      ctx.scale(1, Math.cos(p.wobble));                  // 翻面效果
      ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.h);
      ctx.restore();
    }
    for (const p of parts) {
      if (p.kind !== "emoji") continue;
      p.rot += p.vr * dt;
      ctx.globalAlpha = Math.min(1, (p.ttl - p.life) / 0.6, p.life / 0.15);
      ctx.save();
      ctx.translate(p.x + Math.sin(p.life * 2.2 + p.phase) * p.sway, p.y);
      ctx.rotate(p.rot);
      ctx.font = `${p.size}px ${EMOJI_FONT}`;
      ctx.textAlign = "center"; ctx.textBaseline = "middle";
      ctx.fillText(p.text, 0, 0);
      ctx.restore();
    }
    ctx.globalAlpha = 1;
    requestAnimationFrame(loop);
  }

  function floater(emoji, who, delay = 0) {
    setTimeout(() => {
      const el = document.createElement("div");
      el.className = "floater";
      el.style.left = `${rand(8, 88)}%`;
      const emo = Object.assign(document.createElement("div"), { className: "emo", textContent: emoji });
      el.append(emo);
      if (who) el.append(Object.assign(document.createElement("div"), { className: "who", textContent: who }));
      el.addEventListener("animationend", () => el.remove());
      layer.append(el);
      while (layer.children.length > MAX_FLOATERS) layer.firstChild.remove();
    }, delay);
  }

  // ------------------------------------------------------------ 声音
  function unlock() {  // 浏览器要求用户点击后才能出声，在“点击开始”时调用
    if (!actx) {
      actx = new (window.AudioContext || window.webkitAudioContext)();
      master = actx.createGain();
      master.gain.value = 0.7 * volume;
      master.connect(actx.destination);
      noise = actx.createBuffer(1, actx.sampleRate * 2, actx.sampleRate);
      const data = noise.getChannelData(0);
      for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
    }
    actx.resume();
  }

  function setVolume(v) {
    volume = v;
    if (master) master.gain.value = 0.7 * v;
  }

  function voice(seconds) {
    if (!actx || actx.state !== "running" || voices >= MAX_VOICES) return false;
    voices++;
    setTimeout(() => voices--, seconds * 1000);
    return true;
  }

  function noiseBurst(t, { freq, q = 1, type = "bandpass", gain, attack = 0.002, decay }) {
    const src = actx.createBufferSource();
    src.buffer = noise;
    const filter = actx.createBiquadFilter();
    filter.type = type; filter.frequency.value = freq; filter.Q.value = q;
    const g = actx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(gain, t + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t + attack + decay);
    src.connect(filter).connect(g).connect(master);
    src.start(t, Math.random() * 1.5);
    src.stop(t + attack + decay + 0.05);
    return filter;
  }

  // 一下拍手：几个间隔极短的噪声脉冲叠在一起，最后一个带短混响尾巴
  function clapAt(t, gain) {
    const freq = rand(1000, 1700);
    noiseBurst(t, { freq, q: 1.2, gain: gain * 0.6, decay: 0.01 });
    noiseBurst(t + 0.009, { freq, q: 1.2, gain: gain * 0.6, decay: 0.01 });
    noiseBurst(t + 0.018, { freq, q: 1.0, gain, decay: rand(0.09, 0.14) });
  }

  function note(t, freq, decay, gain, type) {  // 一个快速起音、指数衰减的音
    const osc = actx.createOscillator(), g = actx.createGain();
    osc.type = type; osc.frequency.value = freq;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(gain, t + 0.005);
    g.gain.exponentialRampToValueAtTime(0.0001, t + decay);
    osc.connect(g).connect(master);
    osc.start(t); osc.stop(t + decay + 0.05);
  }

  function tone(t, f0, f1, f2, dur, gain) {  // 一声口哨：音高先滑到 f1 再滑到 f2，带颤音
    const osc = actx.createOscillator();
    osc.frequency.setValueAtTime(f0, t);
    osc.frequency.exponentialRampToValueAtTime(f1, t + dur * 0.55);
    osc.frequency.exponentialRampToValueAtTime(f2, t + dur);
    const lfo = actx.createOscillator(), depth = actx.createGain();
    lfo.frequency.value = 7; depth.gain.value = 25;
    lfo.connect(depth).connect(osc.frequency);
    const g = actx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(gain, t + 0.04);
    g.gain.setValueAtTime(gain, t + dur - 0.06);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    osc.connect(g).connect(master);
    osc.start(t); lfo.start(t); osc.stop(t + dur + 0.05); lfo.stop(t + dur + 0.05);
    noiseBurst(t, { freq: 6000, type: "highpass", q: 0.5, gain: 0.015, attack: 0.03, decay: dur });  // 气声
  }

  const sound = {
    applause() {  // 一阵掌声：约 80 下拍手，开头密集、逐渐稀疏变小
      if (!voice(3.4)) return;
      const t0 = actx.currentTime;
      for (let i = 0; i < 80; i++) {
        const dt = 3 * Math.pow(Math.random(), 1.6);
        clapAt(t0 + dt, 0.35 * (1 - dt / 3.3) * rand(0.5, 1));
      }
    },
    whistle() {  // 起哄口哨：一声上扬，再一声上扬后下滑
      if (!voice(1.2)) return;
      const t = actx.currentTime;
      tone(t, 900, 2200, 2400, 0.35, 0.22);
      tone(t + 0.45, 1300, 2500, 900, 0.6, 0.22);
    },
    ding() {  // 点赞：清脆的“叮—咚”
      if (!voice(1)) return;
      const t = actx.currentTime;
      note(t, 1318.5, 0.6, 0.16, "sine"); note(t, 2637, 0.3, 0.05, "sine");
      note(t + 0.13, 1760, 0.7, 0.16, "sine"); note(t + 0.13, 3520, 0.3, 0.04, "sine");
    },
    sweet() {  // 比心：柔和的上行琶音 C E G C
      if (!voice(1.2)) return;
      const t = actx.currentTime;
      [523.25, 659.25, 783.99, 1046.5].forEach((f, i) => note(t + i * 0.11, f, 0.55, 0.11, "triangle"));
    },
    boing() {  // 哈哈：三声卡通弹簧“嘣”
      if (!voice(0.9)) return;
      const t0 = actx.currentTime;
      for (let i = 0; i < 3; i++) {
        const t = t0 + i * 0.17, osc = actx.createOscillator(), g = actx.createGain();
        osc.frequency.setValueAtTime(260, t);
        osc.frequency.exponentialRampToValueAtTime(720 - i * 60, t + 0.06);
        osc.frequency.exponentialRampToValueAtTime(230, t + 0.16);
        g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.2, t + 0.01);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.17);
        osc.connect(g).connect(master); osc.start(t); osc.stop(t + 0.2);
      }
    },
    sparkle() {  // 送花：从高到低一串闪亮的音
      if (!voice(1)) return;
      const t = actx.currentTime;
      [2093, 1760, 1568, 1318.5, 1174.7, 1046.5, 880, 784].forEach((f, i) => note(t + i * 0.06, f, 0.35, 0.06, "triangle"));
    },
    clink() {  // 干杯：玻璃碰撞，几个不成谐波关系的高频分音快速衰减
      if (!voice(1)) return;
      const t = actx.currentTime;
      [2100, 3380, 4710, 5930].forEach((f, i) => note(t, f, 0.8 - i * 0.15, 0.07, "sine"));
      [2250, 3600].forEach(f => note(t + 0.11, f, 0.4, 0.035, "sine"));
    },
    whoosh(seconds) {  // 烟花升空：带通噪声由低到高扫频
      if (!voice(seconds)) return;
      const t = actx.currentTime;
      const f = noiseBurst(t, { freq: 400, q: 2, gain: 0.05, attack: 0.1, decay: seconds });
      f.frequency.exponentialRampToValueAtTime(2600, t + seconds);
    },
    boom() {  // 烟花爆炸：低频闷响 + 噼里啪啦的碎响
      if (!voice(1.6)) return;
      const t = actx.currentTime;
      noiseBurst(t, { freq: 320, type: "lowpass", q: 0.7, gain: 0.7, attack: 0.005, decay: 1.2 });
      const osc = actx.createOscillator(), g = actx.createGain();
      osc.frequency.setValueAtTime(90, t); osc.frequency.exponentialRampToValueAtTime(35, t + 0.4);
      g.gain.setValueAtTime(0.5, t); g.gain.exponentialRampToValueAtTime(0.0001, t + 0.45);
      osc.connect(g).connect(master); osc.start(t); osc.stop(t + 0.5);
      for (let i = 0; i < 16; i++) noiseBurst(t + rand(0.25, 1.4), { freq: 5000, type: "highpass", gain: rand(0.03, 0.08), decay: 0.02 });
    },
  };

  // ------------------------------------------------------------ 对外接口
  function react(kind, who) {
    who = who || "有人";
    if (kind === "clap") {
      confetti("left"); confetti("right");
      floater("👏", who);
      for (let i = 1; i <= 4; i++) floater("👏", "", i * 180);
      sound.applause();
    } else if (kind === "whistle") {
      floater("😙", who);
      ["🎵", "🎶", "🎵", "🎶"].forEach((n, i) => floater(n, "", 150 + i * 200));
      sound.whistle();
    } else if (kind === "like") {
      floater("👍", who); likeBurst(); sound.ding();
    } else if (kind === "heart") {
      floater("❤️", who); hearts(); sound.sweet();
    } else if (kind === "haha") {
      floater("😂", who); laughs(); sound.boing();
    } else if (kind === "flower") {
      floater("🌹", who); flowers(); sound.sparkle();
    } else if (kind === "cheers") {
      floater("🍻", who); cheers();
    } else if (kind === "firework") {
      floater("🎆", who);
      [0, 380, 760].forEach(d => setTimeout(launch, d));
    }
  }

  return { init, unlock, setVolume, react };
})();
