'use strict';

const { spawn } = require('node:child_process');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');

const DEFAULT_TARGET_BUNDLE = 'com.frostyeve.enconvo';
const DEFAULT_TARGET_NAME = 'EnConvo';

class EnconvoAudioMonitor extends EventEmitter {
  constructor(options = {}) {
    super();
    this.helperPath = options.helperPath || '';
    this.spawnProcess = options.spawnProcess || spawn;
    this.fileExists = options.fileExists || fs.existsSync;
    this.platform = options.platform || process.platform;
    this.retryMs = options.retryMs || 2500;
    this.targetBundleID = options.targetBundleID || DEFAULT_TARGET_BUNDLE;
    this.targetName = options.targetName || DEFAULT_TARGET_NAME;
    this.simulate = Boolean(options.simulate);
    this.logger = typeof options.logger === 'function' ? options.logger : () => {};
    this.enabled = false;
    this.status = 'off';
    this.message = 'Follow EnConvo is off.';
    this.processCount = 0;
    this.child = null;
    this.stdoutBuffer = '';
    this.retryTimer = null;
    this.lastErrorCode = null;
    this.stoppingChildren = new Set();
  }

  snapshot() {
    return {
      enabled: this.enabled,
      status: this.status,
      message: this.message,
      processCount: this.processCount,
      target: this.targetName,
    };
  }

  setEnabled(value) {
    const enabled = Boolean(value);
    if (enabled === this.enabled) return this.snapshot();
    this.enabled = enabled;
    if (enabled) this.start();
    else this.stop();
    return this.snapshot();
  }

  start() {
    if (!this.enabled || this.child || this.retryTimer) return;
    if (this.platform !== 'darwin') {
      this.setStatus('unsupported', 'Follow EnConvo requires macOS 14.2 or later.');
      return;
    }
    if (!this.helperPath || !this.fileExists(this.helperPath)) {
      this.setStatus('unavailable', 'The EnConvo audio helper is unavailable.');
      return;
    }

    this.lastErrorCode = null;
    this.stdoutBuffer = '';
    this.setStatus('starting', 'Connecting to EnConvo audio…');
    const args = [
      '--bundle-id', this.targetBundleID,
      '--name', this.targetName,
    ];
    if (this.simulate) args.push('--simulate');

    let child;
    try {
      child = this.spawnProcess(this.helperPath, args, {
        stdio: ['ignore', 'pipe', 'pipe'],
      });
    } catch (error) {
      this.lastErrorCode = 'spawn_failed';
      this.setStatus('error', `Unable to start audio monitoring: ${error.message || error}`);
      return;
    }
    this.child = child;

    child.stdout?.setEncoding('utf8');
    child.stdout?.on('data', (chunk) => this.consumeStdout(chunk));
    child.stderr?.setEncoding('utf8');
    child.stderr?.on('data', (chunk) => this.logger(`[audio tap] ${chunk}`));
    child.once('error', (error) => {
      if (this.child !== child) return;
      this.lastErrorCode = 'spawn_failed';
      this.setStatus('error', `Unable to start audio monitoring: ${error.message || error}`);
    });
    child.once('exit', (code, signal) => this.handleExit(child, code, signal));
  }

  stop() {
    if (this.retryTimer) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
    const child = this.child;
    this.child = null;
    this.stdoutBuffer = '';
    this.lastErrorCode = null;
    this.processCount = 0;
    if (child) {
      this.stoppingChildren.add(child);
      child.kill('SIGTERM');
    }
    this.setStatus('off', 'Follow EnConvo is off.');
  }

  dispose() {
    this.enabled = false;
    this.stop();
    this.removeAllListeners();
  }

  consumeStdout(chunk) {
    this.stdoutBuffer += chunk;
    if (this.stdoutBuffer.length > 1_000_000) {
      this.stdoutBuffer = this.stdoutBuffer.slice(-100_000);
    }
    const lines = this.stdoutBuffer.split('\n');
    this.stdoutBuffer = lines.pop() || '';
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        this.handleMessage(JSON.parse(trimmed));
      } catch {
        this.logger(`[audio tap] ignored malformed output: ${trimmed.slice(0, 180)}\n`);
      }
    }
  }

  handleMessage(value) {
    if (!value || typeof value !== 'object') return;
    if (value.type === 'ready') {
      this.lastErrorCode = null;
      this.processCount = Number.isFinite(value.processCount) ? value.processCount : 0;
      this.setStatus('ready', 'Listening for EnConvo audio.');
      return;
    }
    if (value.type === 'sample') {
      if (!this.enabled || this.status !== 'ready') return;
      const sample = {
        rms: finite(value.rms),
        peak: finite(value.peak),
        low: finite(value.low),
        mid: finite(value.mid),
        high: finite(value.high),
        zcr: finite(value.zcr),
        active: Boolean(value.active),
        timestamp: finite(value.timestamp),
      };
      this.emit('sample', sample);
      return;
    }
    if (value.type !== 'error') return;
    this.lastErrorCode = String(value.code || 'unknown');
    const message = String(value.message || 'EnConvo audio monitoring failed.');
    if (this.lastErrorCode === 'target_not_running') {
      this.setStatus('waiting', 'Waiting for EnConvo audio…');
    } else if (this.lastErrorCode === 'permission_or_audio_error') {
      this.setStatus('permission', message);
    } else if (this.lastErrorCode === 'unsupported') {
      this.setStatus('unsupported', message);
    } else {
      this.setStatus('error', message);
    }
  }

  handleExit(child, code, signal) {
    if (this.stoppingChildren.delete(child)) return;
    if (this.child !== child) return;
    this.child = null;
    this.logger(`[audio tap exited] code=${code} signal=${signal}\n`);
    if (!this.enabled) return;
    const retryable = this.lastErrorCode === 'target_not_running'
      || (!this.lastErrorCode && code !== 4 && code !== 5);
    if (retryable) {
      this.setStatus('waiting', 'Waiting for EnConvo audio…');
      this.retryTimer = setTimeout(() => {
        this.retryTimer = null;
        this.start();
      }, this.retryMs);
    }
  }

  setStatus(status, message) {
    const changed = status !== this.status || message !== this.message;
    this.status = status;
    this.message = message;
    if (changed) this.emit('state', this.snapshot());
  }
}

function finite(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

module.exports = {
  DEFAULT_TARGET_BUNDLE,
  DEFAULT_TARGET_NAME,
  EnconvoAudioMonitor,
};