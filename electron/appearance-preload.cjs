'use strict';

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('vivieenAppearance', Object.freeze({
  getState: () => ipcRenderer.invoke('vivieen:get-state'),
  setSize: (value) => ipcRenderer.invoke('vivieen:set-pet-zoom', Number(value)),
  setOpacity: (value) => ipcRenderer.invoke('vivieen:set-pet-opacity', Number(value)),
  onState: (callback) => {
    if (typeof callback !== 'function') return () => {};
    const listener = (_event, payload) => callback(payload);
    ipcRenderer.on('vivieen:state', listener);
    return () => ipcRenderer.removeListener('vivieen:state', listener);
  },
}));
