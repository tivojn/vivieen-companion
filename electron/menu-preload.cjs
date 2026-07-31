'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('vivieenMenu', {
  onSpec: (callback) => ipcRenderer.on('vivieen:menu-spec', (_event, value) => callback(value)),
  size: (value) => ipcRenderer.send('vivieen:menu-size', value),
  action: (id) => ipcRenderer.send('vivieen:menu-action', String(id || '')),
  close: () => ipcRenderer.send('vivieen:menu-close'),
});
