'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync, spawnSync } = require('node:child_process');

const APP_ID = 'com.vivieen.companion';

function inspectSignature(target) {
  const result = spawnSync('/usr/bin/codesign', ['-dv', '--verbose=4', target], {
    encoding: 'utf8',
  });
  if (result.status !== 0) {
    throw new Error(`Unable to inspect ${target}:\n${result.stderr || result.stdout}`);
  }
  return `${result.stdout || ''}\n${result.stderr || ''}`;
}

function sign(target, identity, options = {}) {
  const args = ['--force', '--options', 'runtime'];
  if (identity !== '-') args.push('--timestamp');
  if (options.identifier) args.push('--identifier', options.identifier);
  if (options.entitlements) args.push('--entitlements', options.entitlements);
  args.push('--sign', identity, target);
  execFileSync('/usr/bin/codesign', args, { stdio: 'inherit' });
}

module.exports = async function afterSign(context) {
  if (context.electronPlatformName !== 'darwin') return;

  const projectDir = context.packager.projectDir;
  const productName = context.packager.appInfo.productFilename;
  const appPath = path.join(context.appOutDir, `${productName}.app`);
  const helperPath = path.join(
    appPath,
    'Contents',
    'Resources',
    'native',
    'enconvo-audio-tap',
  );
  if (!fs.existsSync(helperPath)) {
    throw new Error(`Embedded audio capture executable is missing: ${helperPath}`);
  }

  const appSignature = inspectSignature(appPath);
  const authority = appSignature.match(/^Authority=(Developer ID Application:[^\r\n]+)$/m);
  const helperSignature = inspectSignature(helperPath);
  if (!authority) {
    if (!helperSignature.includes(`Identifier=${APP_ID}`)) {
      throw new Error(`Embedded audio capture identity is not ${APP_ID}`);
    }
    return;
  }

  const identity = authority[1];
  const appEntitlements = path.join(projectDir, 'build', 'entitlements.mac.plist');
  const helperEntitlements = path.join(projectDir, 'build', 'entitlements.mac.inherit.plist');

  sign(helperPath, identity, {
    identifier: APP_ID,
    entitlements: helperEntitlements,
  });
  sign(appPath, identity, { entitlements: appEntitlements });

  const finalSignature = inspectSignature(helperPath);
  if (!finalSignature.includes(`Identifier=${APP_ID}`)) {
    throw new Error(`Embedded audio capture identity is not ${APP_ID}`);
  }
  execFileSync('/usr/bin/codesign', [
    '--verify',
    '--deep',
    '--strict',
    '--verbose=2',
    appPath,
  ], { stdio: 'inherit' });
};
