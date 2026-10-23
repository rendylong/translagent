'use strict';
const {contextBridge, ipcRenderer} = require('electron');

contextBridge.exposeInMainWorld('desk', {
  isDesktop: true,
  platform: process.platform,
  electronVersion: process.versions.electron,
  getScreenPermission: () => ipcRenderer.invoke('screen-permission'),
  diag: msg => ipcRenderer.invoke('rec-diag', msg),
  openPermissionGuide: () => ipcRenderer.invoke('open-permission-guide'),
  startDragApp: () => ipcRenderer.invoke('start-drag-app'),
  relaunchApp: () => ipcRenderer.invoke('relaunch-app'),
  openAppFolder: () => ipcRenderer.invoke('open-app-folder')
});
