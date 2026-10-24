'use strict';
/* 实时字幕悬浮条窗口:加载 /live?mode=bar 的无边框置顶半透明小窗。
 * 设计约束:
 *  - 关闭只隐藏(hide),字幕会话(麦克风采集 + WS)不中断;仅 before-quit / 主窗关闭时销毁。
 *  - 页面(live.js)在窗内自采音频,隐藏时必须不节流,否则音频图停摆(与 main.js 录音窗同一硬要求)。
 *  - 页面自带的字幕条拖动只移动 DOM 元素,移不动无边框窗口——由注入的
 *    -webkit-app-region 接管为原生窗口拖动,窗口位置由主进程持久化。
 *  - 页面各阶段(setup 表单 / bar 字幕条 / panel 完整面板 / end 结束卡)高度差异大,
 *    用注入的 MutationObserver → document.title('live:<phase>') 通知主进程调窗尺寸,
 *    不改 static/ 一行。
 */
const {app, BrowserWindow, screen} = require('electron');
const fs = require('fs');
const path = require('path');

let liveWin = null;

/* 与 main.js 同款运行日志(launchd 拉起时 stdout 会丢) */
function log(){
  const line = new Date().toISOString().slice(11, 19) + ' [live-win] '
    + Array.from(arguments).join(' ');
  console.log('[desktop]', Array.from(arguments).join(' '));
  try{ fs.appendFileSync(path.join(app.getPath('userData'), 'main.log'), line + '\n'); }catch(e){}
}

/* ---------- 窗口位置与分阶段尺寸记忆(userData/live-window.json) ---------- */
/* macOS 上 transparent 窗口被系统禁止原生边缘缩放(Electron 官方文档 Limitations),
   缩放由注入的自绘手柄经 resize-live IPC 驱动 setSize;用户调过的尺寸按阶段记住,
   resizeForPhase 只在阶段切换时套用,不再覆盖用户尺寸。 */

function statePath(){
  return path.join(app.getPath('userData'), 'live-window.json');
}

function loadState(){
  try{
    const p = JSON.parse(fs.readFileSync(statePath(), 'utf8'));
    const onScreen = screen.getAllDisplays().some(d =>
      p.x >= d.workArea.x && p.x < d.workArea.x + d.workArea.width &&
      p.y >= d.workArea.y && p.y < d.workArea.y + d.workArea.height);
    if(Number.isFinite(p.x) && Number.isFinite(p.y) && onScreen) return p;
  }catch(e){}
  return null;
}

function savePos(){
  if(!liveWin || liveWin.isDestroyed()) return;
  try{
    const [x, y] = liveWin.getPosition();
    const prev = loadState() || {};
    fs.writeFileSync(statePath(), JSON.stringify({x, y, sizes: prev.sizes || {}}));
  }catch(e){}
}

let curPhase = 'bar';          // 当前页面阶段(page-title-updated 维护)
let progResizeAt = 0;          // 最近一次程序化 setBounds 时刻(区分用户缩放)
let saveSizeTimer = 0;

function scheduleSizeSave(){
  // 记住当前阶段的用户尺寸(500ms 防抖,手柄拖动只落最终值)
  if(!liveWin || liveWin.isDestroyed()) return;
  clearTimeout(saveSizeTimer);
  saveSizeTimer = setTimeout(() => {
    try{
      const [w, h] = liveWin.getSize();
      const prev = loadState() || {};
      const sizes = Object.assign({}, prev.sizes);
      sizes[curPhase] = [w, h];
      const [x, y] = liveWin.getPosition();
      fs.writeFileSync(statePath(), JSON.stringify({x, y, sizes}));
    }catch(e){}
  }, 500);
}

function noteUserSize(){
  // 兜底:原生途径的缩放(resized 事件)也记忆;程序化 setBounds 不算
  if(Date.now() - progResizeAt < 400) return;
  scheduleSizeSave();
}

/* ---------- 页面阶段 → 窗口尺寸档位 ---------- */

const PHASE_SIZE = {
  setup: [560, 500],   // 开始表单卡片
  bar:   [560, 150],   // 字幕条:操作按钮悬停在条上方 top:-34px,150 高才不裁
  panel: [920, 700],   // 完整面板(页面 .panel 名义 880×640)
  end:   [560, 450]    // 结束卡
};
const PHASE_MIN = {    // 手柄缩放的下限(上限由屏幕工作区夹住)
  setup: [420, 420],
  bar:   [420, 110],
  panel: [540, 360],
  end:   [420, 380]
};

function resizeForPhase(phase){
  if(!liveWin || liveWin.isDestroyed() || !PHASE_SIZE[phase]) return;
  const saved = loadState();
  const user = saved && saved.sizes && saved.sizes[phase];
  const [w, h] = Array.isArray(user) ? user : PHASE_SIZE[phase];
  const wa = screen.getDisplayMatching(liveWin.getBounds()).workArea;
  let [x, y] = liveWin.getPosition();
  const cw = Math.min(w, wa.width), ch = Math.min(h, wa.height);
  if(y + ch > wa.y + wa.height) y = Math.max(wa.y, wa.y + wa.height - ch);  // 长大时别伸出屏幕底
  if(x + cw > wa.x + wa.width) x = Math.max(wa.x, wa.x + wa.width - cw);
  progResizeAt = Date.now();
  liveWin.setBounds({x, y, width: cw, height: ch});
}

function clampSizeFor(phase, w, h){
  const [minW, minH] = PHASE_MIN[phase] || [360, 100];
  const wa = screen.getPrimaryDisplay().workArea;
  return [Math.round(Math.max(minW, Math.min(w, wa.width))),
          Math.round(Math.max(minH, Math.min(h, wa.height)))];
}

function resizeLiveTo(w, h){
  if(!liveWin || liveWin.isDestroyed()) return;
  const [cw, ch] = clampSizeFor(curPhase, w, h);
  progResizeAt = Date.now();
  liveWin.setSize(cw, ch);
  scheduleSizeSave();   // 手柄拖动是用户意图,直接记忆(不走 noteUserSize 的程序化守卫)
}

/* ---------- 注入页面:悬浮形态 + 原生窗口拖动 ---------- */

const PAGE_CSS = `
  body{background:transparent !important}
  #bar{left:6px !important; right:6px !important; top:6px !important; bottom:6px !important;
       width:auto !important; height:auto !important; transform:none !important;
       padding-top:34px !important; -webkit-app-region:drag}
  #barActions, #barActions *{-webkit-app-region:no-drag}
  #barActions{top:5px !important; right:6px !important; opacity:1 !important; pointer-events:auto !important}
  #barLog{flex:1 1 auto !important; max-height:none !important; min-height:0 !important;
          -webkit-app-region:no-drag}
  #barGrab{display:none !important}
  .phead{-webkit-app-region:drag}
  .phead *{-webkit-app-region:no-drag}
  .panel{left:0 !important; top:0 !important; width:100% !important; height:100% !important;
         max-width:none !important; border-radius:0 !important; box-shadow:none !important}
  /* 表单卡与结束卡:窗口拖动区(交互控件除外)——这两个阶段整窗没有其他 drag 区 */
  .card{-webkit-app-region:drag}
  .card input, .card select, .card button, .card label{-webkit-app-region:no-drag}
  .ecard{-webkit-app-region:drag}
  .ecard a, .ecard button{-webkit-app-region:no-drag}
  /* 自绘缩放手柄(transparent 窗口无原生边缘缩放);字幕条已铺满窗口,恒钉窗角 */
  #nativeResizeGrip{position:fixed; right:0; bottom:0; width:22px; height:22px;
    z-index:9999; cursor:nwse-resize; -webkit-app-region:no-drag;
    background:linear-gradient(135deg, transparent 0 44%, rgba(127,127,127,.55) 44% 56%,
      transparent 56% 69%, rgba(127,127,127,.55) 69% 81%, transparent 81%)}
`;

/* 页面无主进程事件通道,借 document.title 桥接阶段变化 */
const PAGE_PHASE_WATCH = `
(function(){
  var prev = null;
  function phase(){
    var s = document.querySelector('#setup'),
        st = document.querySelector('#stage'),
        e  = document.querySelector('#endwrap');
    if (e && !e.hidden) return 'end';
    if (s && !s.hidden) return 'setup';
    if (st && !st.hidden) return document.querySelector('#bar').hidden ? 'panel' : 'bar';
    return 'setup';
  }
  function report(){
    var p = phase();
    if (p !== prev){ prev = p; document.title = 'live:' + p; }
  }
  new MutationObserver(report).observe(document.documentElement,
    {subtree: true, attributes: true, attributeFilter: ['hidden']});
  report();
})()`;

/* 自绘缩放手柄:仅在悬浮窗环境(window.desk 由 preload 注入)装配;
 * 指针拖动算出目标窗口尺寸 → desk.resizeLive(w,h) → 主进程 setSize。
 * move/up 同时挂在手柄(配 pointer capture,出窗仍跟踪)与 document(合成事件/无
 * capture 时兜底)上;16ms 限频天然去重两路重复回调。 */
const PAGE_RESIZE_GRIP = `
(function(){
  if(!window.desk || !window.desk.resizeLive) return;
  var grip = document.createElement('div');
  grip.id = 'nativeResizeGrip';
  grip.title = '拖动调整窗口大小';
  document.body.appendChild(grip);
  grip.addEventListener('pointerdown', function(ev){
    if(ev.button !== 0) return;
    ev.preventDefault(); ev.stopPropagation();
    var sx = ev.clientX, sy = ev.clientY;
    var sw = window.outerWidth, sh = window.outerHeight;
    var last = 0;
    try{ grip.setPointerCapture(ev.pointerId); }catch(e){}
    function move(e2){
      var now = performance.now();
      if(now - last < 16) return;          // ~60fps 限频,IPC 不刷屏
      last = now;
      window.desk.resizeLive(sw + e2.clientX - sx, sh + e2.clientY - sy);
    }
    function up(e3){
      try{ grip.releasePointerCapture(e3.pointerId); }catch(e){}
      grip.removeEventListener('pointermove', move);
      grip.removeEventListener('pointerup', up);
      document.removeEventListener('pointermove', move, true);
      document.removeEventListener('pointerup', up, true);
    }
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', up);
    document.addEventListener('pointermove', move, true);
    document.addEventListener('pointerup', up, true);
  });
})()`;

/* ---------- 窗口生命周期 ---------- */

let ipcHooked = false;

function createLiveCaptionWindow(serverUrl, mainWin){
  if(liveWin && !liveWin.isDestroyed()) return liveWin;
  const wa = screen.getPrimaryDisplay().workArea;
  const saved = loadState();
  liveWin = new BrowserWindow({
    x: saved ? saved.x : Math.round(wa.x + (wa.width - 560) / 2),
    y: saved ? saved.y : Math.round(wa.y + wa.height - 150),
    width: 560,
    height: 150,
    frame: false,
    alwaysOnTop: true,
    transparent: true,
    hasShadow: false,
    resizable: true,   // transparent 窗口原生边缘缩放被系统禁用,实际缩放走自绘手柄 + resize-live
    skipTaskbar: true,
    show: false,
    title: '实时字幕',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      // preload 注入 desk 桥:resizeLive(手柄缩放)/openMainJob(结束卡回工作台)
      preload: path.join(__dirname, 'preload.js'),
      // 字幕条隐藏时页面仍持续采音,后台节流会停摆音频图 → 字幕静默断流
      backgroundThrottling: false
    }
  });
  if(!ipcHooked){
    ipcHooked = true;
    const {ipcMain} = require('electron');
    ipcMain.handle('resize-live', (ev, w, h) => {
      if(Number.isFinite(w) && Number.isFinite(h)) resizeLiveTo(w, h);
      return true;
    });
    // 结束卡「关闭」:隐藏悬浮窗(与窗口 close 同语义,会话已结束,不销毁)
    ipcMain.handle('hide-live', () => {
      if(liveWin && !liveWin.isDestroyed()) liveWin.hide();
      return true;
    });
  }
  // 构造项的 alwaysOnTop 文档类型是 Boolean,level 字符串会被静默丢弃
  // (源码按真值硬编码 floating 档);压过其他应用的全屏空间必须用带 level
  // 签名的 setAlwaysOnTop 显式升到 screen-saver 档
  liveWin.setAlwaysOnTop(true, 'screen-saver');
  liveWin.on('closed', () => { liveWin = null; });
  // 关闭 = 隐藏,会话不中断;真正销毁只走 destroyLiveWindow
  liveWin.on('close', e => {
    if(liveWin.__forceClose || liveWin.isDestroyed()) return;
    e.preventDefault();
    liveWin.hide();
  });
  liveWin.on('moved', savePos);
  liveWin.on('resized', noteUserSize);   // 兜底:任何原生途径的缩放也记住
  liveWin.once('ready-to-show', () => liveWin.show());

  const wc = liveWin.webContents;
  wc.on('did-finish-load', () => {
    wc.insertCSS(PAGE_CSS).catch(e => log('insertCSS 失败', e && e.message));
    wc.executeJavaScript(PAGE_PHASE_WATCH, true)
      .catch(e => log('阶段监视注入失败', e && e.message));
    wc.executeJavaScript(PAGE_RESIZE_GRIP, true)
      .catch(e => log('缩放手柄注入失败', e && e.message));
  });
  wc.on('did-fail-load', (e, code, desc, url, isMain) => {
    if(!isMain) return;
    log('live 页面加载失败', code, String(desc), '—— 3 秒后重试');
    setTimeout(() => {
      if(liveWin && !liveWin.isDestroyed()) liveWin.webContents.reload();
    }, 3000);
  });
  liveWin.on('page-title-updated', (e, title) => {
    if(!title.startsWith('live:')) return;
    const phase = title.slice(5);
    if(phase !== curPhase){ curPhase = phase; resizeForPhase(phase); }
  });

  liveWin.loadURL(serverUrl + '/live?mode=bar')
    .catch(e => log('loadURL 失败', e && e.message));

  // 主窗关闭即应用退出:销毁悬浮窗,否则隐藏窗口会卡住 window-all-closed(Dock 残留)
  if(mainWin && !mainWin.isDestroyed() && !mainWin.__liveWinHooked){
    mainWin.__liveWinHooked = true;
    mainWin.once('closed', () => destroyLiveWindow());
  }
  return liveWin;
}

function toggleLiveCaptionWindow(serverUrl, mainWin){
  if(!liveWin || liveWin.isDestroyed()){
    createLiveCaptionWindow(serverUrl, mainWin);
    return;
  }
  if(liveWin.isVisible()) liveWin.hide();
  else { liveWin.show(); liveWin.focus(); }
}

/* 主界面入口用:确保悬浮窗存在且可见(不是 toggle,已开着就只聚焦) */
function ensureLiveCaptionWindow(serverUrl, mainWin){
  const w = createLiveCaptionWindow(serverUrl, mainWin);
  if(liveWin && !liveWin.isDestroyed() && !liveWin.isVisible()) liveWin.show();
  if(liveWin && !liveWin.isDestroyed()) liveWin.focus();
  return w || liveWin;
}

function destroyLiveWindow(){
  if(liveWin && !liveWin.isDestroyed()){
    liveWin.__forceClose = true;
    liveWin.destroy();
  }
  liveWin = null;
}

/* Dock 菜单入口:右键 Dock 图标 → 实时字幕悬浮条(main.js 原无菜单结构,这是最小侵入的挂载点) */
function installDockMenu(serverUrl, getMainWin){
  try{
    const {Menu} = require('electron');
    app.dock.setMenu(Menu.buildFromTemplate([
      {label: '实时字幕悬浮条', click: () => toggleLiveCaptionWindow(serverUrl, getMainWin())}
    ]));
    return true;
  }catch(e){
    log('Dock 菜单注册失败', e && e.message);
    return false;
  }
}

app.on('before-quit', destroyLiveWindow);

module.exports = {createLiveCaptionWindow, toggleLiveCaptionWindow, ensureLiveCaptionWindow,
                  destroyLiveWindow, installDockMenu};
