'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('vivieenBubble', Object.freeze({
  onText: (callback) => {
    if (typeof callback !== 'function') return () => {};
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('vivieen:bubble-text', listener);
    return () => ipcRenderer.removeListener('vivieen:bubble-text', listener);
  },
}));