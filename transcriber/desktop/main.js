'use strict';
const {app, BrowserWindow, desktopCapturer, dialog, ipcMain, nativeImage, session, shell} = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const {spawn, spawnSync} = require('child_process');
const {installDockMenu} = require('./live-window');

const SERVER_URL = 'http://127.0.0.1:8765';
const REPORT_PATH = path.join(__dirname, 'selftest-report.json');
const SELF_TEST = process.argv.includes('--self-test');

// 本应用包根(.app 目录);dev 模式下退化为预编译 Electron.app 本体
function appRoot(){
  return path.dirname(path.dirname(path.dirname(process.execPath)));
}

// 授权引导直达:隐私与安全性主页(「系统声音录制」在其中的独立条目)
const PANE_URL = 'x-apple.systempreferences:com.apple.preference.security?Privacy';
const FALLBACK_PANE_URL = 'x-apple.systempreferences:com.apple.ScreenCapture-Settings.extension';

// macOS 26 系统声音:CATap 优先(独立的「系统声音录制」权限),SCK 路线兜底
app.commandLine.appendSwitch('enable-features',
  'MacCatapSystemAudioLoopbackCapture,MacSckSystemAudioLoopbackOverride,MacLoopbackAudioForScreenShare');

// 生命周期状态机:waiting-server → ready(窗口加载完成)→ running;
// waiting-server 失败的分支是 offline(错误页,可重试后重新进入 waiting-server)。
const PHASES = ['waiting-server', 'ready', 'running', 'offline'];
let phase = 'waiting-server';
let win = null;
let mainRecording = false;   // 主窗录音状态(rec-state 广播维护),深链跳转时防打断
let booting = false;

function setPhase(next){
  if(!PHASES.includes(next) || phase === next) return;
  phase = next;
  log('phase ->', phase);
}

/* 运行日志:launchd 启动时 stdout 会丢,落盘到 userData/main.log */
function log(){
  const line = new Date().toISOString().slice(11, 19) + ' ' + Array.from(arguments).join(' ');
  console.log('[desktop]', Array.from(arguments).join(' '));
  try{ fs.appendFileSync(path.join(app.getPath('userData'), 'main.log'), line + '\n'); }catch(e){}
}

function sleep(ms){ return new Promise(r => setTimeout(r, ms)); }

function probeServer(){
  return new Promise(resolve => {
    const req = http.get(SERVER_URL + '/api/jobs', res => {
      res.resume();
      resolve(res.statusCode < 500);
    });
    req.on('error', () => resolve(false));
    req.setTimeout(2000, () => { req.destroy(); resolve(false); });
  });
}

async function waitServer(){
  for(let i = 0; i < 30; i++){
    if(await probeServer()) return true;
    await sleep(1000);
  }
  return false;
}

function errorDataUrl(){
  const html = [
    '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    '<title>无法连接转写服务</title><style>',
    'body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;',
    'background:#f6f7f8;color:#1b1e23;font:14px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}',
    '.card{max-width:430px;margin:20px;padding:34px 38px;background:#fff;border:1px solid #e5e7ea;',
    'border-radius:10px;text-align:center}',
    'h1{font-size:16px;margin:0 0 12px}',
    'p{margin:0 0 8px;color:#575f6b;text-align:left}',
    'button{margin-top:18px;height:36px;padding:0 24px;border:0;border-radius:6px;background:#1e2126;',
    'color:#fff;font-size:14px;font-weight:600;cursor:pointer}',
    'button:hover{background:#33373d}',
    '</style></head><body><div class="card">',
    '<h1>无法连接本地转写服务</h1>',
    '<p>已等待 30 秒,工作台服务(http://127.0.0.1:8765)仍未响应。</p>',
    '<p>请先双击 transcriber 目录里的「启动转写服务.command」启动服务;'
    + '或直接使用「启动桌面客户端.command」,它会自动拉起服务。</p>',
    '<p>服务启动后点击下方重试,或稍候片刻本窗口会自动恢复。</p>',
    '<button onclick="location.reload()">重试</button>',
    '</div></body></html>'
  ].join('');
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(html);
}

function createWindow(){
  win = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 980,
    minHeight: 700,
    show: false,
    title: '会议录制与转录工作台',
    backgroundColor: '#f6f7f8',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // 录音客户端的硬要求:窗口隐藏/最小化时 Chromium 默认节流后台页,
      // 音频图会停摆,长会议录音会静默丢数据
      backgroundThrottling: false
    }
  });
  win.on('closed', () => { win = null; });
  // 录音保护:关窗前先问渲染页是否在录音;拦截必须同步,确认后再强制关闭
  win.on('close', (e) => {
    if(win.__forceClose || win.isDestroyed()) return;
    e.preventDefault();
    (async () => {
      let recording = false, t = '';
      try{
        recording = await win.webContents.executeJavaScript(
          'document.body.classList.contains("recording")', true);
        if(recording){
          t = await win.webContents.executeJavaScript(
            '(document.querySelector("#rec-time")||{}).textContent||""', true);
        }
      }catch(err){ /* 页面不可用则照常关闭 */ }
      if(!recording){ win.__forceClose = true; win.close(); return; }
      const {response} = await dialog.showMessageBox(win, {
        type: 'warning',
        buttons: ['丢弃录音并关闭', '继续录音'],
        defaultId: 1, cancelId: 1,
        message: '正在录音(' + t.trim() + ')',
        detail: '关闭窗口将丢弃整场录音,且无法恢复。'
      });
      if(response === 0){
        setDockRecording(false);
        win.__forceClose = true;
        win.close();
      }
    })();
  });
  attachPageHandlers(win);
}

function showIfNormal(){
  // 自测也要显示窗口:从未显示过的窗口,Chromium 不启动音频渲染,采不到样
  if(win && !win.isDestroyed() && !win.isVisible()) win.show();
}

function attachPageHandlers(){
  const wc = win.webContents;

  wc.on('did-start-navigation', (e, url, isInPlace, isMainFrame) => {
    if(SELF_TEST || isInPlace || !isMainFrame) return;
    // offline 时用户点了错误页的「重试」(location.reload 重新导航到同一 data: URL)
    if(phase === 'offline' && url.startsWith('data:')) boot();
  });

  wc.on('did-finish-load', () => {
    showIfNormal();
    if(SELF_TEST) return;
    const url = wc.getURL();
    if(!url.startsWith('data:') && phase === 'waiting-server'){
      setPhase('ready');
      wc.executeJavaScript('document.readyState', true)
        .then(v => { if(v && phase === 'ready') setPhase('running'); })
        .catch(() => {});
    }
  });

  wc.once('ready-to-show', showIfNormal);
}

function watchRecovery(){
  // offline 期间每 2 秒探活,服务起来了就自动重进 waiting-server,不逼用户点重试
  if(phase !== 'offline') return;
  setTimeout(async () => {
    if(phase !== 'offline') return;
    if(await probeServer()) boot();
    else watchRecovery();
  }, 2000);
}

async function boot(){
  if(booting || !win) return;
  booting = true;
  try{
    setPhase('waiting-server');
    let up = false;
    if(await waitServer()){
      try{
        await win.loadURL(SERVER_URL + '/');
        up = true;
      }catch(e){
        console.error('[desktop] 页面加载失败,转错误页', e);
      }
    }
    if(!up){
      await win.loadURL(errorDataUrl());
      setPhase('offline');
      watchRecovery();
    }
  }catch(e){
    console.error('[desktop] boot failed', e);
  }finally{
    booting = false;
  }
}

/* ---------- 自测模式:产出 selftest-report.json,退出码 0/1 ---------- */

function blankAudio(extra){
  return Object.assign({attempted: false, sampleCount: 0, bytes: 0, error: null}, extra || {});
}

// 测试代码整体注入页面执行,只走 window.MeetRecorder.Recorder 公开契约
function pageTestScript(source, recordMs){
  return `(async () => {
    const source = ${JSON.stringify(source)};
    const out = {attempted: true, trackEnded: false, sampleCount: 0, bytes: 0, error: null};
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    let rec = null;
    let restoreGdm = null;
    try{
      if(source === 'system' && navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia){
        const orig = navigator.mediaDevices.getDisplayMedia.bind(navigator.mediaDevices);
        navigator.mediaDevices.getDisplayMedia = async cs => {
          const s = await orig(cs);
          for(const t of s.getAudioTracks()) t.addEventListener('ended', () => { out.trackEnded = true; });
          return s;
        };
        restoreGdm = () => { navigator.mediaDevices.getDisplayMedia = orig; };
      }
      if(!(window.MeetRecorder && window.MeetRecorder.Recorder)){
        throw new Error('window.MeetRecorder.Recorder 不存在');
      }
      rec = new window.MeetRecorder.Recorder({source: source, onLevel: () => { out.sampleCount++; }});
      await rec.start();
      await sleep(${recordMs});
      const res = await rec.stop();
      const blob = res instanceof Blob ? res
        : (res && (res.blob || res.file || res.audio || res.data)) || null;
      out.bytes = blob && typeof blob.size === 'number' ? blob.size : 0;
      if(restoreGdm) restoreGdm();
    }catch(e){
      out.error = String((e && e.message) || e);
      try{ if(rec && rec.abort) await rec.abort(); }catch(_){}
      try{ if(restoreGdm) restoreGdm(); }catch(_){}
    }
    return out;
  })()`;
}

async function runPageTest(source, recordMs, timeoutMs){
  let timer = null;
  try{
    return await Promise.race([
      win.webContents.executeJavaScript(pageTestScript(source, recordMs), true),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('超时 ' + timeoutMs + 'ms(可能在等系统权限弹窗)')), timeoutMs);
      })
    ]);
  }catch(e){
    return blankAudio({attempted: true, error: String((e && e.message) || e)});
  }finally{
    clearTimeout(timer);
  }
}

/* getDisplayMedia 需要真实用户激活;聚焦 + 注入真实点击,并回读激活状态 */
function syntheticClick(){
  if(win && !win.isDestroyed()){
    win.show();
    if(win.focus) win.focus();
    const wc = win.webContents;
    const ev = {type: 'mouseDown', x: 60, y: 60, button: 'left', clickCount: 1};
    wc.sendInputEvent(ev);
    wc.sendInputEvent({...ev, type: 'mouseUp'});
  }
}

/* 产品路径复现:真实点击页面上的录音按钮(带用户激活),走完整条用户流程 */
async function productFlowTest(){
  const out = {};
  const wc = win.webContents;
  const pos = await wc.executeJavaScript(`(() => {
    const sel = document.querySelector('#rec-src');
    if(!sel || !Array.from(sel.options).some(o => o.value === 'system')) return null;
    sel.value = 'system';
    document.querySelector('#tab-rec').click();
    const r = document.querySelector('#rec-start').getBoundingClientRect();
    return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
  })()`, true);
  if(!pos){ out.skipped = '页面无系统声音选项(非 Electron 环境)'; return out; }
  if(win && !win.isDestroyed()){ win.show(); if(win.focus) win.focus(); }
  const clickAt = (x, y) => {
    wc.sendInputEvent({type: 'mouseMove', x, y});
    wc.sendInputEvent({type: 'mouseDown', x, y, button: 'left', clickCount: 1});
    wc.sendInputEvent({type: 'mouseUp', x, y, button: 'left', clickCount: 1});
  };
  clickAt(pos.x, pos.y);
  await sleep(3500);
  out.afterStart = await wc.executeJavaScript(`(() => ({
    state: (typeof rec !== 'undefined' && rec) ? rec.state : 'none',
    error: (typeof rec !== 'undefined' && rec && rec.error) ? rec.error : null,
    time: document.querySelector('#rec-time').textContent,
    src: document.querySelector('#rec-src').value,
    recordingClass: document.body.classList.contains('recording')
  }))()`, true).catch(e => ({evalErr: String(e && e.message || e)}));
  out.guideOpen = !!(guideWin && !guideWin.isDestroyed());
  if(out.afterStart.state === 'recording'){
    const stop = await wc.executeJavaScript(`(() => {
      const r = document.querySelector('#rec-stop').getBoundingClientRect();
      return {x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)};
    })()`, true).catch(() => null);
    if(stop) clickAt(stop.x, stop.y);   // 停止 → 打包 WAV → 上传
    await sleep(5000);
    out.afterStop = await wc.executeJavaScript(`(() => ({
      state: (typeof rec !== 'undefined' && rec) ? rec.state : 'none',
      recordingClass: document.body.classList.contains('recording')
    }))()`, true).catch(() => null);
  }
  log('productFlow:', JSON.stringify(out));
  return out;
}

// 裸 executeJavaScript 的超时护栏:getDisplayMedia 可能因 TCC 待授权无限悬置
// (重打包换 ad-hoc 身份后权限失效即触发),与 runPageTest 同款保护
function evalWithTimeout(promise, ms, tag){
  let timer = null;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(tag + ' 超时 ' + ms + 'ms(可能在等系统授权)')), ms);
    })
  ]).finally(() => clearTimeout(timer));
}

async function runSelfTest(){
  const report = {
    serverReachable: false,
    pageLoaded: false,
    systemAudio: blankAudio({trackEnded: false}),
    mic: blankAudio(),
    ts: null
  };
  const finish = ok => {
    report.ts = new Date().toISOString();
    try{ fs.writeFileSync(REPORT_PATH, JSON.stringify(report, null, 2)); }catch(e){
      console.error('[desktop] 写报告失败', e);
    }
    console.log('[desktop] self-test', ok ? 'PASS' : 'FAIL', REPORT_PATH);
    app.exit(ok ? 0 : 1);
  };

  try{
    report.serverReachable = await waitServer();
    if(!report.serverReachable){
      report.systemAudio.error = '服务器 30 秒内未就绪';
      report.mic.error = '服务器 30 秒内未就绪';
      return finish(false);
    }
    try{
      await win.loadURL(SERVER_URL + '/');
      report.pageLoaded = true;
    }catch(e){
      report.systemAudio.error = '页面加载失败: ' + String((e && e.message) || e);
      report.mic.error = '页面未加载,跳过';
      return finish(false);
    }

    await sleep(1000);
    const deskCheck = await win.webContents.executeJavaScript(`(() => {
      const d = window.desk || {};
      return {
        hasDesk: !!d.isDesktop,
        api: ['getScreenPermission','openPermissionGuide','startDragApp','relaunchApp','openAppFolder']
          .map(k => k + ':' + (typeof d[k])),
        screenPermission: null
      };
    })()`, true).catch(() => ({hasDesk: false}));
    try{
      deskCheck.screenPermission = await win.webContents.executeJavaScript(
        'window.desk.getScreenPermission()', true);
    }catch(e){
      deskCheck.screenPermission = 'error: ' + String(e && e.message || e);
    }
    report.desk = deskCheck;
    // 原始 getDisplayMedia(注入真实点击获得用户激活,否则 pre-handler 即拒),诊断系统声音链路用
    syntheticClick();
    report.userActivation = await win.webContents.executeJavaScript(
      'navigator.userActivation ? navigator.userActivation.isActive : "unavailable"', true);
    // 纯音频 display capture:只碰「系统声音录制」(CATap)权限,不碰屏幕录制
    report.audioOnly = await evalWithTimeout(win.webContents.executeJavaScript(`(async () => {
      try{
        const s = await navigator.mediaDevices.getDisplayMedia({audio: true, video: false});
        const at = s.getAudioTracks();
        const n = {ok: true, audio: at.length, aLabel: at[0] && at[0].label,
                   aSettings: at[0] && at[0].getSettings ? at[0].getSettings() : null};
        await new Promise(r => setTimeout(r, 800));
        let nonZero = false;
        try{
          const ctx = new AudioContext({sampleRate: 16000});
          const src = ctx.createMediaStreamSource(s);
          const an = ctx.createAnalyser(); an.fftSize = 2048; src.connect(an);
          const buf = new Float32Array(an.fftSize);
          for(let i = 0; i < 8 && !nonZero; i++){
            await new Promise(r => setTimeout(r, 150));
            an.getFloatTimeDomainData(buf);
            for(let k = 0; k < buf.length; k++) if(buf[k] !== 0){ nonZero = true; break; }
          }
          ctx.close();
        }catch(e){ n.analyserErr = String(e.message); }
        n.samplesFlowing = nonZero;
        s.getTracks().forEach(t => t.stop());
        return n;
      }catch(e){
        return {ok: false, name: e.name, message: String(e.message || '').slice(0, 200)};
      }
    })()`, true), 30000, 'audioOnly').catch(e => ({ok: false, name: 'eval-failed', message: String(e && e.message || e)}));
    report.rawDisplayMedia = await evalWithTimeout(win.webContents.executeJavaScript(`(async () => {
      try{
        const s = await navigator.mediaDevices.getDisplayMedia({audio: true, video: true});
        const n = {ok: true, audio: s.getAudioTracks().length, video: s.getVideoTracks().length,
                   aLabel: s.getAudioTracks()[0] && s.getAudioTracks()[0].label};
        s.getTracks().forEach(t => t.stop());
        return n;
      }catch(e){
        return {ok: false, name: e.name, message: String(e.message || '').slice(0, 200)};
      }
    })()`, true), 30000, 'rawDisplayMedia').catch(e => ({ok: false, name: 'eval-failed', message: String(e && e.message || e)}));
    syntheticClick();
    Object.assign(report.systemAudio, await runPageTest('system', 3000, 20000));
    Object.assign(report.mic, await runPageTest('mic', 2000, 15000));
    report.productFlow = await productFlowTest();

    const audioOk = a => !a.error && (a.bytes > 0 || a.sampleCount > 0);
    finish(report.pageLoaded && audioOk(report.systemAudio) && audioOk(report.mic));
  }catch(e){
    console.error('[desktop] self-test crashed', e);
    finish(false);
  }
}

/* ---------- 服务自启动 ---------- */

function serverAppPath(){
  const root = appRoot();
  const candidates = [
    path.join(root, '..', 'transcriber', 'app.py'),
    path.join(root, '..', '..', 'transcriber', 'app.py')
  ];
  return candidates.find(p => fs.existsSync(p)) || null;
}

function pickPython(){
  // 从 Finder/Dock 启动时 launchd 的 PATH 只含系统目录,bare python3 会解析到
  // 没装 flask/sherpa_onnx 的系统解释器。按优先级探测能 import flask 的解释器。
  const candidates = [
    process.env.TRANSLAGENT_PYTHON,
    '/usr/local/bin/python3',
    '/opt/homebrew/bin/python3',
    'python3'
  ].filter(Boolean);
  for(const py of candidates){
    try{
      const r = spawnSync(py, ['-c', 'import flask'], {encoding: 'utf8', timeout: 15000});
      if(r.status === 0) return py;
    }catch(e){ /* 试下一个 */ }
  }
  return 'python3';
}

async function ensureServer(){
  if(await probeServer()) return true;
  const py = serverAppPath();
  if(!py){
    console.error('[desktop] 找不到 transcriber/app.py,无法自动启动服务');
    return false;
  }
  console.log('[desktop] 拉起转写服务', py);
  const child = spawn(pickPython(), [py], {cwd: path.dirname(py), detached: true, stdio: 'ignore'});
  child.unref();
  return true;
}

/* ---------- 授权引导窗(置顶小窗,单例) ---------- */

let guideWin = null;

function openGuideWindow(){
  if(guideWin && !guideWin.isDestroyed()){
    guideWin.focus();
    return;
  }
  guideWin = new BrowserWindow({
    width: 380,
    height: 480,
    resizable: false,
    minimizable: false,
    alwaysOnTop: true,          // 系统设置打开时引导窗必须仍可见、可拖出
    title: '允许录制系统声音',
    backgroundColor: '#f6f7f8',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  guideWin.on('closed', () => { guideWin = null; });
  guideWin.loadFile(path.join(__dirname, 'guide.html')).catch(e => {
    console.error('[desktop] 引导窗加载失败', e);
  });
}

async function openPermissionGuide(){
  const ok = await shell.openExternal(PANE_URL).catch(() => false);
  if(!ok) await shell.openExternal(FALLBACK_PANE_URL).catch(() => {});
  openGuideWindow();
}

function setDockRecording(on){
  try{ app.dock.setBadge(on ? '●' : ''); }catch(e){ /* 非 macOS 无 dock */ }
}

function registerIpc(){
  ipcMain.handle('screen-permission', () => {
    const st = require('electron').systemPreferences.getMediaAccessStatus('screen');
    log('screen-permission IPC →', st);
    return st;
  });
  ipcMain.handle('rec-diag', (ev, msg) => {
    log('renderer:', String(msg).slice(0, 300));
    // 渲染页录音状态广播 → Dock 角标
    if(msg === 'rec-state:1'){ mainRecording = true; setDockRecording(true); }
    else if(msg === 'rec-state:0'){ mainRecording = false; setDockRecording(false); }
    return true;
  });
  ipcMain.handle('open-permission-guide', () => { log('open-permission-guide(引导被唤起)'); openPermissionGuide(); return true; });
  ipcMain.handle('start-drag-app', (ev) => {
    const icon = nativeImage.createFromPath(path.join(__dirname, 'app-icon-128.png'));
    ev.sender.startDrag({file: appRoot(), icon: icon.isEmpty() ? undefined : icon});
    return true;
  });
  ipcMain.handle('relaunch-app', () => {
    app.relaunch();
    app.exit(0);
    return true;
  });
  ipcMain.handle('open-app-folder', () => {
    shell.openPath(appRoot());
    return true;
  });
  // 主界面「实时字幕」入口:拉起/聚焦悬浮窗(区别于 Dock 菜单的 toggle,已开只聚焦)
  ipcMain.handle('open-live-caption', () => {
    const {ensureLiveCaptionWindow} = require('./live-window');
    ensureLiveCaptionWindow(SERVER_URL, win);
    return true;
  });
  // 悬浮窗结束卡「打开文稿」:聚焦主窗并深链到对应任务(主窗在录音时只聚焦防打断)
  ipcMain.handle('open-main-job', (ev, jobId) => {
    if(!win || win.isDestroyed()) return false;
    if(win.isMinimized()) win.restore();
    win.show();
    win.focus();
    const id = String(jobId || '').replace(/[^\w-]/g, '');
    if(id && !mainRecording) win.webContents.loadURL(SERVER_URL + '/#' + id);
    return true;
  });
}

/* ---------- 装配 ---------- */

function onReady(){
  registerIpc();
  ensureServer();
  // useSystemPicker(macOS 15+):由系统原生选择器接管;音频优先路径不走它,handler 兜底
  session.defaultSession.setDisplayMediaRequestHandler((req, cb) => {
    log('display-media handler:', 'videoRequested=' + req.videoRequested, 'audioRequested=' + req.audioRequested);
    if(!req.videoRequested && req.audioRequested){
      // 纯音频:Core Audio Tap,只碰「系统声音录制」权限,不需要屏幕源
      cb({audio: 'loopback'});
      return;
    }
    desktopCapturer.getSources({types: ['screen']})
      .then(s => {
        cb({video: s[0], audio: 'loopback'});
      })
      .catch(e => { log('getSources 失败', e && e.message); cb(null); });
  });

  session.defaultSession.setPermissionRequestHandler((wc, permission, cb) => {
    // getDisplayMedia 走的是独立的 display-capture 权限类型,不是 media,漏掉会被秒拒
    if(permission !== 'media' && permission !== 'display-capture') return cb(false);
    try{
      if(new URL(wc.getURL()).hostname === '127.0.0.1') return cb(true);
    }catch(e){}
    cb(false);
  });

  createWindow();
  installDockMenu(SERVER_URL, () => win);   // 实时字幕悬浮条入口:右键 Dock 图标
  if(SELF_TEST) runSelfTest();
  else boot();
}

const gotLock = SELF_TEST || app.requestSingleInstanceLock();
if(!gotLock){
  app.quit();
}else{
  app.on('second-instance', () => {
    if(!win) return;
    if(win.isMinimized()) win.restore();
    win.focus();
  });
  app.whenReady().then(onReady);
  app.on('window-all-closed', () => app.quit());
}
