#!/usr/bin/env node
/**
 * patch-config-schema.js
 *
 * Runs as the `postexport-dynamic` npm lifecycle hook after
 * `janus-cli package export-dynamic-plugin --clean` regenerates dist-dynamic/.
 *
 * janus-cli collects config schemas from package node_modules but does NOT
 * automatically include the plugin package's own schema from config.d.ts.
 * This script injects the govHub schema entry (with visibility: frontend) into
 * dist-dynamic/dist/.config-schema.json so the RHDH backend forwards
 * govHub.url to the browser — without this, the plugin throws:
 *   Error: Missing required config value at 'govHub.url' in 'app'
 */
'use strict';

const fs = require('fs');
const path = require('path');

const SCHEMA_PATH = path.join(
  __dirname,
  '..',
  'dist-dynamic',
  'dist',
  '.config-schema.json',
);

const GOV_HUB_ENTRY = {
  packageName: '@mcombi/backstage-plugin-governance-hub',
  path: 'config.d.ts',
  value: {
    $schema: 'http://json-schema.org/draft-07/schema#',
    type: 'object',
    properties: {
      govHub: {
        type: 'object',
        description: 'Governance Hub plugin configuration',
        properties: {
          url: {
            type: 'string',
            visibility: 'frontend',
            description:
              'URL of the Governance Hub service embedded in the RHDH iframe.' +
              ' Injected via GOVERNANCE_HUB_URL from the rhdh-secrets Secret.',
          },
        },
        required: ['url'],
      },
    },
  },
};

if (!fs.existsSync(SCHEMA_PATH)) {
  console.warn(
    `[patch-config-schema] ${SCHEMA_PATH} not found — skipping patch`,
  );
  process.exit(0);
}

const data = JSON.parse(fs.readFileSync(SCHEMA_PATH, 'utf8'));
const schemas = data.schemas || [];

const alreadyPresent = schemas.some(
  s => s.packageName === GOV_HUB_ENTRY.packageName,
);

if (alreadyPresent) {
  console.log(
    '[patch-config-schema] govHub schema already present — no change needed',
  );
} else {
  schemas.unshift(GOV_HUB_ENTRY);
  data.schemas = schemas;
  fs.writeFileSync(SCHEMA_PATH, JSON.stringify(data, null, 2));
  console.log(
    `[patch-config-schema] Injected govHub schema (total schemas: ${schemas.length})`,
  );
}
